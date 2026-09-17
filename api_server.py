"""Authenticated contract review API with bounded uploads and durable task results."""
import asyncio
import base64
import csv
import io
import json
import os
import secrets
import sys
import tempfile
import threading
import uuid
import zipfile
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Dict, Any, Optional
import uvicorn
from fastapi import FastAPI, File, UploadFile, Form, HTTPException, Request, Query
from fastapi.responses import StreamingResponse, FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from agent.pipeline import ContractReviewAgent, ReviewCancelled
from knowledge_base.retriever import KnowledgeBase, DOMAIN_LABELS
from knowledge_base.router import infer_contract_type
from core.task_store import TaskStore
from core.web_auth import COOKIE_NAME, credentials_match, issue_session, valid_session
from config import (PROJECT_DIR, AUTH_USER, AUTH_PASS, API_HOST, API_PORT, LLM_MODEL, REVIEW_MODEL_OPTIONS, MAX_UPLOAD_BYTES,
                    MAX_DOCX_EXPANDED_BYTES, REVIEW_MAX_WORKERS, REVIEW_MAX_PENDING,
                    KB_REVIEW_DOMAINS)

# Windows 后台启动并重定向到日志文件时，默认代码页可能是 GBK；审核进度和
# 模型统计含 Unicode 符号，若不统一编码会在 print 阶段中断整个审核任务。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, 'reconfigure'):
        _stream.reconfigure(encoding='utf-8', errors='backslashreplace')

STATIC_DIR = str(PROJECT_DIR / 'static')
OUTPUT_DIR = str(PROJECT_DIR / 'output' / 'reviews')
LLM_CLIENT, KB, STORE = None, None, None
singleton_lock = threading.RLock()
pool_lock = threading.RLock()
executor = ThreadPoolExecutor(max_workers=max(1, REVIEW_MAX_WORKERS))
task_pool = {}
slots = threading.BoundedSemaphore(max(1, REVIEW_MAX_PENDING))


def get_store():
    global STORE
    with singleton_lock:
        if STORE is None:
            STORE = TaskStore(Path(OUTPUT_DIR).parent / 'tasks.sqlite3')
        return STORE


def get_llm_client():
    global LLM_CLIENT
    with singleton_lock:
        if LLM_CLIENT is None:
            from core.llm_client import LLMClient
            LLM_CLIENT = LLMClient()
        return LLM_CLIENT


def get_kb():
    global KB
    with singleton_lock:
        if KB is None:
            kb = KnowledgeBase(llm_client=get_llm_client())
            kb.load()
            KB = kb
        return KB


@asynccontextmanager
async def lifespan(app):
    if not AUTH_PASS or len(AUTH_PASS) < 12:
        raise RuntimeError('请在.env.local配置至少12位的AUTH_PASS；系统不提供默认密码。')
    Path(OUTPUT_DIR).mkdir(parents=True, exist_ok=True)
    get_store().interrupt_unfinished()
    await asyncio.to_thread(get_kb)
    yield


app = FastAPI(title='合同智能审核系统API', version='3.0.0', lifespan=lifespan)


class BodyLimitMiddleware:
    def __init__(self, app):
        self.app = app
    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        total = 0
        async def bounded_receive():
            nonlocal total
            message = await receive()
            if message['type']=='http.request':
                total += len(message.get('body', b''))
                if total > MAX_UPLOAD_BYTES + 1024*1024:
                    raise HTTPException(413, '请求体超过上传大小限制')
            return message
        await self.app(scope, bounded_receive, send)


app.add_middleware(BodyLimitMiddleware)


@app.middleware('http')
async def basic_auth(request: Request, call_next):
    if request.url.path == '/health':
        return await call_next(request)
    if not AUTH_PASS:
        return JSONResponse(status_code=503, content={'detail':'尚未配置登录密码'})
    auth = request.headers.get('Authorization','')
    ok = valid_session(request.cookies.get(COOKIE_NAME), AUTH_USER, AUTH_PASS)
    if auth.startswith('Basic '):
        try:
            user, _, password = base64.b64decode(auth[6:], validate=True).decode().partition(':')
            ok = ok or credentials_match(user, password, AUTH_USER, AUTH_PASS)
        except (ValueError, UnicodeError):
            pass
    if request.method not in ('GET','HEAD','OPTIONS'):
        origin = request.headers.get('Origin')
        if origin and origin.rstrip('/') != str(request.base_url).rstrip('/'):
            return JSONResponse(status_code=403, content={'detail':'不接受跨站请求'})
    length = request.headers.get('Content-Length')
    if length and (not length.isdigit() or int(length)>MAX_UPLOAD_BYTES+1024*1024):
        return JSONResponse(status_code=413, content={'detail':'上传大小超过限制'})
    public_login = request.url.path in ('/login', '/api/auth/login')
    if not ok and not public_login:
        if request.method in ('GET', 'HEAD') and not request.url.path.startswith('/api/'):
            return RedirectResponse('/login', status_code=303, headers={'Cache-Control':'no-store'})
        return JSONResponse(status_code=401, content={'detail':'登录已失效，请重新登录'},
                            headers={'Cache-Control':'no-store'})
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'same-origin'
    response.headers['Cache-Control'] = 'no-store'
    return response


@app.get('/login')
async def login_page():
    return FileResponse(os.path.join(STATIC_DIR, 'login.html'))


@app.post('/api/auth/login')
async def web_login(request: Request, username: str=Form(..., max_length=200),
                    password: str=Form(..., max_length=1024)):
    if not credentials_match(username, password, AUTH_USER, AUTH_PASS):
        return JSONResponse(status_code=401, content={'detail':'用户名或密码不正确'})
    response = JSONResponse({'ok':True})
    response.set_cookie(COOKIE_NAME, issue_session(AUTH_USER, AUTH_PASS),
                        httponly=True, secure=request.url.scheme=='https', samesite='strict', path='/')
    return response


@app.post('/api/auth/logout')
async def web_logout():
    response = RedirectResponse('/login', status_code=303)
    response.delete_cookie(COOKIE_NAME, path='/', httponly=True, samesite='strict')
    return response


class ReviewTask:
    def __init__(self, task_id, file_path, filename, params):
        self.task_id, self.file_path, self.filename, self.params = task_id, file_path, filename, params
        self.status, self.result, self.error, self.steps = 'pending', None, None, []
        self.created_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        self.cancel_event = threading.Event()


def canonical_id(value):
    try:
        return str(uuid.UUID(value))
    except (ValueError,TypeError,AttributeError):
        raise HTTPException(400, '任务编号无效')


@app.get('/health')
async def health_check():
    return {'status':'ok', 'message':'API服务正常运行'}


@app.get('/api/system/status')
async def system_status():
    llm = get_llm_client()
    available = await asyncio.to_thread(llm.is_available)
    kb = get_kb()
    counts = {d:sum(kb._matches(r) for r in kb.metadata_by_domain.get(d, []))
              for d in KB_REVIEW_DOMAINS if d in kb.metadata_by_domain}
    list_models = getattr(llm, 'list_models', None)
    installed_names = await asyncio.to_thread(list_models) if callable(list_models) else [getattr(llm, 'model', '')]
    installed = {name.lower() for name in installed_names}
    model_options = [
        {'value':name, 'label':settings['label'], 'num_ctx':settings['num_ctx'],
         'installed':name.lower() in installed, 'default':name == LLM_MODEL}
        for name, settings in REVIEW_MODEL_OPTIONS.items()
    ]
    return {'llm_available':available, 'llm_model':getattr(llm,'model',LLM_MODEL),
            'review_models':model_options,
            'kb_loaded':kb.is_ready(), 'kb_domains':counts, 'kb_total':sum(counts.values()),
            'kb_review_domains':list(counts),
            'kb_source_total':sum(len(v) for v in kb.metadata_by_domain.values()),
            'vector_indexes':list(kb.indexes), 'kb_warnings':kb.warnings,
            'time':datetime.now().isoformat(timespec='seconds')}


def validate_docx(path):
    try:
        with zipfile.ZipFile(path) as z:
            files = z.infolist()
            if len(files)>5000 or sum(i.file_size for i in files)>MAX_DOCX_EXPANDED_BYTES:
                raise HTTPException(413, 'Word解压后的内容超过限制')
            if 'word/document.xml' not in z.namelist() or '[Content_Types].xml' not in z.namelist():
                raise HTTPException(400, '文件不是有效的Word文档')
            if any(i.flag_bits & 1 for i in files):
                raise HTTPException(400, '不支持加密Word文档')
            if any('vbaProject' in i.filename for i in files):
                raise HTTPException(400, '不支持含宏的文档')
    except zipfile.BadZipFile:
        raise HTTPException(400, '文件不是有效的docx压缩包')


@app.post('/api/review/upload')
async def upload_and_review(file: UploadFile=File(...), review_mode: str=Form('full'), party: str=Form(''),
                            pay_side: str=Form(''), review_model: str=Form('')):
    name = file.filename or ''
    if not name.lower().endswith('.docx') or len(name)>200 or any(ch in name for ch in ('/','\\',':','\x00')):
        raise HTTPException(400, '请上传文件名不含路径的.docx文档')
    if review_mode not in ('full','rules_only') or party not in ('','甲','乙') or pay_side not in ('','pay','receive'):
        raise HTTPException(400, '审核模式、我方立场或付款方向无效')
    selected_model = review_model or LLM_MODEL
    if selected_model not in REVIEW_MODEL_OPTIONS:
        raise HTTPException(400, '审核模型不在允许列表中')
    # 立场一律手动：pay_side 为空即"未指定"，不从合同文本推定
    we_pay = {'pay':True, 'receive':False}.get(pay_side)
    if not slots.acquire(blocking=False):
        raise HTTPException(429, '审核队列已满，请稍后再试')
    temp_dir = Path(tempfile.mkdtemp(prefix='contract_review_'))
    path = temp_dir/'input.docx'
    submitted = False
    try:
        size = 0
        with path.open('wb') as out:
            while chunk := await file.read(1024*1024):
                size += len(chunk)
                if size>MAX_UPLOAD_BYTES:
                    raise HTTPException(413, '文件超过上传大小限制')
                out.write(chunk)
        await asyncio.to_thread(validate_docx, path)
        task_id = str(uuid.uuid4())
        task = ReviewTask(task_id, str(path), name,
                          {'review_mode':review_mode, 'party':party, 'we_pay':we_pay,
                           'review_model':selected_model})
        get_store().create(task)
        with pool_lock:
            task_pool[task_id] = task
        executor.submit(execute_review, task)
        submitted = True
        return {'task_id':task_id,'status':'pending','message':'审核任务已排队'}
    finally:
        await file.close()
        if not submitted:
            path.unlink(missing_ok=True)
            temp_dir.rmdir()
            slots.release()


def execute_review(task):
    try:
        if task.cancel_event.is_set():
            raise ReviewCancelled()
        task.status = 'running'
        get_store().update(task)
        from core.llm_client import LLMClient
        selected_model = task.params.get('review_model') or LLM_MODEL
        model_settings = REVIEW_MODEL_OPTIONS[selected_model]
        llm = LLMClient(model=selected_model, num_ctx=model_settings['num_ctx'])
        use_llm = task.params['review_mode']=='full'
        degraded = None
        if use_llm and not llm.is_available():
            use_llm = False
            degraded = '生成模型不可用，本次只完成规则与本地知识检索；法律、商业模型分析未执行。'
        def progress(steps):
            task.steps = steps
            get_store().update(task)
        agent = ContractReviewAgent(llm_client=llm, knowledge_base=get_kb(), enable_llm=use_llm,
            enable_kb=True, on_progress=progress, cancelled=task.cancel_event.is_set)
        result = agent.review(task.file_path, our_side=task.params.get('party') or None,
                              we_pay=task.params.get('we_pay'))
        result.update(filename=task.filename, task_id=task.task_id,
            review_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S'), party=task.params['party'],
            requested_mode=task.params['review_mode'], degraded_reason=degraded,
            selected_model=selected_model, model_num_ctx=model_settings['num_ctx'])
        result['export_control'] = export_control(result)
        result['risk_level_text'] = risk_level_text(result['summary'])
        save_review_result(task.task_id, task.filename, result)
        task.result, task.status = result, 'completed'
    except ReviewCancelled:
        task.status, task.error = 'cancelled', '审核已取消'
    except Exception as exc:
        task.status, task.error = 'failed', str(exc)[:500]
    finally:
        try:
            get_store().update(task)
        finally:
            # Only the server-created fixed upload path is cleaned up.
            path = Path(task.file_path)
            path.unlink(missing_ok=True)
            try:
                path.parent.rmdir()
            except OSError:
                pass
            with pool_lock:
                task_pool.pop(task.task_id, None)
            slots.release()


def risk_level_text(summary):
    for key,label in [('high','高风险'),('medium','中风险'),('low','低风险')]:
        if summary.get(key,0):
            return label
    return '仅有提示' if summary.get('info',0) else '本次检查未检出风险'


def export_control(result):
    completeness = result.get('completeness')
    intentional_rules_only = (completeness == 'rules_only' and
                              result.get('requested_mode', result.get('review_mode')) == 'rules_only')
    allowed = completeness == 'complete' or intentional_rules_only
    if allowed:
        reason = '报告可导出；所有结论仍需人工复核。'
    elif completeness == 'partial':
        reason = '模型分析未完整完成，PDF/CSV已禁用；可导出原始JSON排查缺项。'
    else:
        reason = '完整审核降级或覆盖范围不明，PDF/CSV已禁用；可导出原始JSON。'
    return {'polished_report_allowed':allowed, 'raw_json_allowed':True, 'reason':reason}


@app.get('/api/review/status/{task_id}')
async def get_review_status(task_id: str):
    task = get_store().get(canonical_id(task_id))
    if task is None:
        raise HTTPException(404, '任务不存在')
    return {k:task[k] for k in ('task_id','filename','status','steps','result','error')}


@app.post('/api/review/cancel/{task_id}')
async def cancel_review(task_id: str):
    key = canonical_id(task_id)
    with pool_lock:
        task = task_pool.get(key)
        if task is None:
            raise HTTPException(409, '该任务已结束或不在当前进程中')
        task.cancel_event.set()
    return {'message':'取消已请求；正在进行的模型调用结束后停止后续批次。'}


def _load_task_result(task_id):
    key = canonical_id(task_id)
    task = get_store().get(key)
    if task and task['result'] is not None:
        result = dict(task['result'])
        result['export_control'] = result.get('export_control') or export_control(result)
        return result
    for path in Path(OUTPUT_DIR).glob(key+'_*.json'):
        result = json.loads(path.read_text(encoding='utf-8'))
        result['export_control'] = result.get('export_control') or export_control(result)
        return result
    raise HTTPException(404, '审核结果不存在或尚未完成')


@app.get('/api/review/result/{task_id}')
async def get_review_result(task_id: str):
    return await asyncio.to_thread(_load_task_result, task_id)


@app.get('/api/review/history')
async def get_review_history(limit: int=Query(50,ge=1,le=200)):
    def read():
        records = {}
        for path in Path(OUTPUT_DIR).glob('*.json'):
            try:
                data = json.loads(path.read_text(encoding='utf-8'))
                key = str(uuid.UUID(data.get('task_id') or path.name.split('_')[0]))
                when = data.get('review_time','')
                if isinstance(when,(int,float)):
                    when = datetime.fromtimestamp(when).strftime('%Y-%m-%d %H:%M:%S')
                item = {k:data.get(k) for k in ('filename','contract_title','contract_type','summary','review_mode','completeness')}
                item['export_control'] = data.get('export_control') or export_control(data)
                item.update(task_id=key, review_time=when, risk_level_text=data.get('risk_level_text') or risk_level_text(data.get('summary',{})))
                if key not in records or when>records[key]['review_time']:
                    records[key] = item
            except (ValueError,TypeError,OSError,OverflowError):
                continue
        return sorted(records.values(), key=lambda x:x['review_time'], reverse=True)[:limit]
    return {'history':await asyncio.to_thread(read)}


@app.get('/api/kb/domains')
async def kb_domains():
    kb = get_kb()
    return [{'key':d, 'label':label,
             'count':sum(kb._matches(r) for r in kb.metadata_by_domain.get(d, []))
                     if d in KB_REVIEW_DOMAINS else 0,
             'enabled':d in KB_REVIEW_DOMAINS}
            for d,label in DOMAIN_LABELS.items()]


@app.get('/api/kb/search')
async def kb_search(q: str=Query('',max_length=2000), domain: Optional[str]=None,
                    top_k: Optional[int]=Query(None,ge=1,le=1000), mode: str='auto'):
    aliases = {'legal':'legal_kb','industry':'industry_kb','company':'company_kb','template':'template_kb'}
    domain = aliases.get(domain,domain) or None
    if domain and domain not in DOMAIN_LABELS:
        raise HTTPException(400,'知识域无效')
    if domain and domain not in KB_REVIEW_DOMAINS:
        raise HTTPException(400,'知识域当前未启用')
    if mode not in ('auto','keyword'):
        raise HTTPException(400,'检索模式无效')
    kb = get_kb()
    domains = [domain] if domain else [d for d in KB_REVIEW_DOMAINS if d in kb.metadata_by_domain]
    query = q.strip()
    if not query:
        limit = top_k or 1000
        records = [record for d in domains for record in kb.metadata_by_domain.get(d, [])
                   if kb._matches(record)]
        records = records[:limit]
        return {'query':'', 'mode':'browse', 'contract_type':'general', 'total':len(records),
                'results':[{'kb':r['kb_type'],'id':r['knowledge_id'], 'title':r['title'],
                            'content':r['content'], 'source_url':r.get('source_url'),
                            'score':None,
                            'validity':'valid' if r.get('effective_from') or r.get('effective_date') else 'date_unverified'}
                           for r in records]}
    limit = top_k or 12
    result_groups = await asyncio.gather(*[
        asyncio.to_thread(kb.search, query, top_k=limit, kb_type=d, use_vector=mode=='auto')
        for d in domains
    ])
    results = sorted((item for group in result_groups for item in group),
                     key=lambda item: -item['score'])[:limit]
    return {'query':query, 'mode':'hybrid' if any(r['retrieval_mode']=='hybrid' for r in results) else 'keyword',
            'contract_type':infer_contract_type(query), 'total':len(results),
            'results':[{'kb':r['chunk']['kb_type'],'id':r['chunk']['knowledge_id'],
                        'title':r['chunk']['title'],'content':r['chunk']['content'],
                        'source_url':r['chunk'].get('source_url'),'score':round(r['score'],4),
                        'validity':r['validity']} for r in results]}


@app.get('/api/review/export/{task_id}')
async def export_report(task_id: str, format: str='pdf'):
    result = await asyncio.to_thread(_load_task_result, task_id)
    control = result.get('export_control') or export_control(result)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    if format=='json':
        payload = {**result, 'export_control':control}
        prefix = 'report' if control['polished_report_allowed'] else 'INCOMPLETE_raw'
        return download(json.dumps(payload,ensure_ascii=False,indent=2).encode(), 'application/json', f'{prefix}_{timestamp}.json')
    if format not in ('csv','excel','pdf'):
        raise HTTPException(400,'不支持的导出格式')
    if not control['polished_report_allowed']:
        raise HTTPException(409, control['reason'])
    if format in ('csv','excel'):
        return generate_csv_report(result,timestamp)
    if format=='pdf':
        return await generate_pdf_report(result,timestamp)


def download(data, media_type, filename):
    return StreamingResponse(io.BytesIO(data), media_type=media_type,
                             headers={'Content-Disposition':f'attachment; filename={filename}'})


def _register_chinese_font():
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    # 优先系统黑体/雅黑/宋体，失败回退内置 CID 宋体。
    for name,path in [('SimHei','C:/Windows/Fonts/simhei.ttf'),
                      ('MSYaHei','C:/Windows/Fonts/msyh.ttc'),
                      ('SimSun','C:/Windows/Fonts/simsun.ttc'),
                      ('WenQuanYi','/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc')]:
        if os.path.exists(path):
            try:
                if name not in pdfmetrics.getRegisteredFontNames():
                    pdfmetrics.registerFont(TTFont(name,path))
                return name
            except Exception:
                pass
    pdfmetrics.registerFont(UnicodeCIDFont('STSong-Light'))
    return 'STSong-Light'


def _pdf_bytes(result):
    from xml.sax.saxutils import escape
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    font = _register_chinese_font()
    body = ParagraphStyle('body',fontName=font,fontSize=10,leading=16,wordWrap='CJK',spaceAfter=6)
    heading = ParagraphStyle('heading',parent=body,fontSize=15,leading=22,spaceBefore=10)
    small = ParagraphStyle('small',parent=body,fontSize=8,leading=12)
    esc = lambda x:escape(str(x or '')).replace('\n','<br/>')
    states = {'complete':'全部批次完成，结论仍需人工复核','partial':'部分分析未完成','rules_only':'仅规则检查，模型分析未执行'}
    story = [Paragraph('合同审核报告',heading),Paragraph(esc(result.get('filename') or result.get('contract_title')),body),
             Paragraph('审核状态：'+esc(states.get(result.get('completeness'),'历史报告，覆盖范围未记录')),body),
             Paragraph('我方立场：'+esc(result.get('party') or '中立'),body),
             Paragraph('使用限制：本报告由自动化系统生成，风险结论、法规适用和修改建议均需法律专业人员复核。',body)]
    cov = result.get('coverage',{})
    story.append(Paragraph(f"条款/表格块：{cov.get('total_clauses',result.get('clause_count',0))}；法律模型已检查{len(cov.get('legal_checked',[]))}；商业模型已检查{len(cov.get('commercial_checked',[]))}",body))
    for warning in [result.get('degraded_reason'),*result.get('review_errors',[]),
                    *result.get('review_warnings',[]),*result.get('document_warnings',[])]:
        if warning: story.append(Paragraph('注意：'+esc(warning),body))
    evidence_states = {'claim_aligned':'法规主题与结论已机械核对，法律适用仍需复核',
                       'topic_related':'引用仅属相关材料，不能直接支撑结论',
                       'not_applicable':'商业风险判断，需人工复核',
                       'needs_review':'尚无直接法规支撑，作为待核查线索'}
    for key,label in [('rule_risks','规则检查'),('legal_risks','法律及合规线索'),('commercial_risks','商业风险')]:
        story.append(Paragraph(label,heading))
        risks = result.get(key,[])
        if not risks:
            story.append(Paragraph('无风险记录；是否执行请结合上方审核状态及覆盖范围判断。',body))
        for r in risks:
            color = {'高风险':'#992525','中风险':'#9c6600','低风险':'#276738','提示':'#555555'}.get(r.get('level'),'#555555')
            story.append(Paragraph(f"<font color='{color}'>[{esc(r.get('level'))}] {esc(r.get('title'))}</font>",body))
            for caption,value in [('条款',r.get('clause_id') or '全文检查'),('原文',r.get('original_text')),
                ('问题',r.get('description')),('建议',r.get('suggestion')),('参考依据',r.get('legal_basis')),
                ('直接依据ID','、'.join(str(x) for x in r.get('evidence',[]))),
                ('相关材料ID','、'.join(str(x) for x in r.get('related_evidence',[]))),
                ('证据关系',evidence_states.get(r.get('evidence_status'))),
                ('复核状态','需人工复核' if r.get('review_status')=='needs_review' else '规则检查')]:
                if value: story.append(Paragraph(caption+'：'+esc(value),small))
            story.append(Spacer(1,6))
    story.append(Paragraph('知识库参考材料',heading))
    for ev in result.get('evidence',[]):
        story.append(Paragraph(esc(ev.get('evidence_id'))+' '+esc(ev.get('citation') or ev.get('title')),body))
        story.append(Paragraph(esc(ev.get('excerpt') or ev.get('content')),small))
        if ev.get('source_url'): story.append(Paragraph(esc(ev['source_url']),small))
    buffer = io.BytesIO()
    SimpleDocTemplate(buffer,pagesize=A4,leftMargin=45,rightMargin=45,topMargin=40,bottomMargin=40).build(story)
    return buffer.getvalue()


async def generate_pdf_report(result, timestamp):
    return download(await asyncio.to_thread(_pdf_bytes,result), 'application/pdf',f'report_{timestamp}.pdf')


def generate_csv_report(result, timestamp):
    def safe(value):
        text = str(value or '')
        return "'"+text if text.lstrip().startswith(('=','+','-','@','\t','\r')) else text
    out = io.StringIO()
    writer = csv.writer(out)
    states = {'complete':'全部批次完成，仍需人工复核', 'rules_only':'仅规则检查'}
    writer.writerow(['审核状态', states.get(result.get('completeness'), '覆盖范围未确认')])
    for warning in result.get('review_warnings', []):
        writer.writerow(['审核提示', warning])
    writer.writerow(['使用限制', '本报告由自动化系统生成，不替代法律专业人员复核'])
    writer.writerow([])
    writer.writerow(['类别','条款','等级','事项','原文','说明','建议','直接依据','相关材料','证据关系','复核状态'])
    for key,label in [('rule_risks','规则'),('legal_risks','法律线索'),('commercial_risks','商业')]:
        for r in result.get(key,[]):
            writer.writerow([safe(v) for v in [label,r.get('clause_id'),r.get('level'),r.get('title'),
                r.get('original_text'),r.get('description'),r.get('suggestion'),r.get('legal_basis'),
                '、'.join(r.get('related_evidence',[])),r.get('evidence_status'),r.get('review_status')]])
    return download(('\ufeff'+out.getvalue()).encode('utf-8'), 'text/csv',f'report_{timestamp}.csv')


def save_review_result(task_id, filename, result):
    key = canonical_id(task_id)
    Path(OUTPUT_DIR).mkdir(parents=True,exist_ok=True)
    record = {**result,'task_id':key,'filename':filename}
    path = Path(OUTPUT_DIR)/f'{key}_result.json'
    temporary = path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8')
    os.replace(temporary,path)


app.mount('/static',StaticFiles(directory=STATIC_DIR),name='static')


@app.get('/')
async def index():
    return FileResponse(os.path.join(STATIC_DIR,'index.html'))


if __name__=='__main__':
    uvicorn.run('api_server:app',host=API_HOST,port=API_PORT,log_level='info')
