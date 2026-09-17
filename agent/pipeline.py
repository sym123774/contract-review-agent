"""Complete, batched review with source validation and explicit partial/failure states."""
import hashlib
import time
import json
import math
import re
from collections import Counter
from pathlib import Path
from datetime import datetime
from config import (SEVEN_DIMENSIONS, REVIEW_BATCH_CHARS, REVIEW_EVIDENCE_CHARS, LLM_NUM_CTX,
                    LLM_MAX_TOKENS, LLM_ITEM_RETRY_LIMIT, KB_REVIEW_TOP_K_PER_KB, KB_REVIEW_EXCERPT_CHARS)
from core.parser import parse_docx, batch_clauses
from core.rules import (run_rule_engine, RiskItem, RiskLevel, risk_to_dict, build_party_context,
                        tag_by_risk, check_dingjin, normalize_rate_notation)
from core.llm_client import LLMClient
from core.evidence_validation import CATEGORICAL_LEGAL_CLAIM, split_claim_evidence
from core.document_checks import build_document_checklist
from core.template_markers import mark_template_clauses
from knowledge_base.models import RetrievalRequest, as_evidence, source_type_label
from knowledge_base.router import infer_contract_type
from agent.prompts import (build_contract_overview, build_system_prompt, build_review_prompt,
                           build_item_retry_prompt, format_rule_risks_for_prompt)

LEVELS = {'高风险':RiskLevel.HIGH, 'high':RiskLevel.HIGH, '中风险':RiskLevel.MEDIUM, 'medium':RiskLevel.MEDIUM,
          '低风险':RiskLevel.LOW, 'low':RiskLevel.LOW, '提示':RiskLevel.INFO, 'info':RiskLevel.INFO}
CONFIDENCE = {'高':.9, 'high':.9, '中':.7, 'medium':.7, '低':.4, 'low':.4}
SAFE_DEDUPE_TAGS = {'penalty_rate', 'dingjin', 'dispute_clause', 'acceptance_window',
                    'payment_terms', 'prepay_ratio', 'delivery_time', 'invoice',
                    'performance_guarantee', 'missing_clause', 'quality', 'ip_ownership',
                    'confidentiality', 'force_majeure', 'termination', 'liability_exclusion',
                    'amount_mismatch', 'blank_fields', 'payment_selection', 'price_adjustment',
                    'oral_agreement', 'development_failure', 'liability_terms', 'contract_scope'}


class ModelOutputError(ValueError):
    pass


class ReviewCancelled(Exception):
    pass


class ContractReviewAgent:
    def __init__(self, llm_client=None, knowledge_base=None, enable_llm=True, enable_kb=True,
                 on_progress=None, cancelled=None):
        self.llm = llm_client or LLMClient()
        self.kb = knowledge_base
        self.enable_llm, self.enable_kb = enable_llm, enable_kb
        self.on_progress, self.cancelled = on_progress, cancelled or (lambda: False)
        self._reset()

    def _reset(self):
        self.steps, self.parsed, self._started_at = [], None, None
        self.contract_type, self.party_context = 'general', {}
        self.rule_risks, self.legal_risks, self.commercial_risks = [], [], []
        self.evidence_results, self.legal_context = [], ''
        self.raw_legal, self.raw_commercial = '', ''
        self.contract_overview = ''
        self.document_checklist = {}
        self.batch_records, self._offered_evidence, self._active_ids = [], {}, set()
        self._unselected_option_ids = set()
        self._inactive_template_ids = set()
        self._offered_order = []
        self._risk_counter = 0
        self._usage_start = 0

    def _log_step(self, step, status='done', detail=''):
        self.steps.append({'step':step, 'status':status, 'detail':detail})
        if self.on_progress:
            self.on_progress(list(self.steps))
        icon = {"done":"✅", "running":"⏳", "skip":"⏭️", "error":"❌"}.get(status, "•")
        print(f"  {icon} {step}" + (f" - {detail}" if detail else ""))

    def review(self, file_path, our_side=None, we_pay=None):
        import time
        self._reset()
        self._usage_start = len(getattr(self.llm, 'usage_log', []))
        self._started_at = time.time()
        self._log_step('合同解析', 'running')
        self.parsed = parse_docx(file_path)
        mark_template_clauses(self.parsed.get('clauses', []))
        self._unselected_option_ids = self._find_unselected_option_ids(self.parsed.get('clauses', []))
        self._inactive_template_ids = {
            c.get('clause_id') for c in self.parsed.get('clauses', [])
            if c.get('template_status') in ('instruction', 'unselected_option')
        }
        self.contract_type = infer_contract_type(self.parsed['full_text'])
        # 立场只取手动选择：our_side=甲/乙，we_pay=我方是否付款方（None=未指定，不做方向性调级）
        self.party_context = build_party_context(self.parsed['full_text'], our_side, we_pay=we_pay)
        active_clauses = [c for c in self.parsed['clauses']
                          if c.get('clause_id') not in self._inactive_template_ids]
        rule_parsed = dict(self.parsed)
        rule_parsed['clauses'] = active_clauses
        rule_parsed['heading_clauses'] = self.parsed['clauses']
        rule_parsed['full_text'] = '\n'.join(
            (c.get('title', '') + '\n' + c.get('content', '')).strip() for c in active_clauses)
        excluded = len(self.parsed['clauses']) - len(active_clauses)
        detail = f"{len(self.parsed['clauses'])}个文本条款/表格块"
        if excluded:
            detail += f"，排除{excluded}个模板说明/未选备选项"
        self._log_step('合同解析', detail=detail)
        self.rule_risks = run_rule_engine(rule_parsed, self.party_context)
        self._log_step('规则检查', detail=f'发现{len(self.rule_risks)}项，已检查全部可读正文及表格')
        self.contract_overview = build_contract_overview(rule_parsed)
        self.document_checklist = build_document_checklist(rule_parsed, self.contract_type)
        # Reserve output and instructions, then divide the remaining conservative character budget.
        # 只有真实调用模型时才按所选模型缩放批次；规则审查不调用生成模型，
        # 其检索覆盖范围不应因为页面上选择了7B或72B而发生变化。
        model_num_ctx = int(getattr(self.llm, 'num_ctx', LLM_NUM_CTX)) if self.enable_llm else LLM_NUM_CTX
        budget = max(600, int((model_num_ctx-LLM_MAX_TOKENS-1800)/1.5))
        batch_budget = min(REVIEW_BATCH_CHARS, max(300, budget//2))
        evidence_budget = min(REVIEW_EVIDENCE_CHARS, max(200, budget-batch_budget))
        batches = batch_clauses(active_clauses, batch_budget)
        all_evidence = {}
        for n, batch in enumerate(batches, 1):
            if self.cancelled():
                raise ReviewCancelled('审核已取消')
            clause_by_id = {c['clause_id']:c for c in self.parsed['clauses']}
            rendered = []
            for item in batch:
                status = clause_by_id[item['clause_id']].get('template_status', 'active')
                marker = ({'instruction':'【模板说明，不是生效义务】',
                           'unselected_option':'【未选择的模板备选项，不是生效义务】',
                           'template_blank':'【含待填写字段，签署前需确认】'}.get(status, ''))
                rendered.append((marker + '\n' if marker else '') + item['text'])
            text = '\n\n'.join(rendered)
            ids = list(dict.fromkeys(x['clause_id'] for x in batch))
            record = {'batch_id':n, 'clause_ids':ids, 'legal':'not_run', 'commercial':'not_run',
                      'errors':[], 'warnings':[]}
            self.batch_records.append(record)
            self._active_ids = set(ids)
            offered = []
            if self.enable_kb and self.kb and self.kb.is_ready():
                try:
                    per_clause = []
                    for cid in ids:
                        clause = clause_by_id[cid]
                        if clause.get('template_status') in ('instruction', 'unselected_option'):
                            continue
                        query = self.parsed['title'] + '\n' + clause.get('title','') + '\n' + clause.get('content','')
                        per_clause.append(self.kb.retrieve(RetrievalRequest(
                            query=query, contract_type=self.contract_type, top_k_per_kb=1),
                            use_vector=self.enable_llm))
                    overall = self.kb.retrieve(RetrievalRequest(
                        query=self.parsed['title']+'\n'+text, contract_type=self.contract_type,
                        top_k_per_kb=KB_REVIEW_TOP_K_PER_KB), use_vector=self.enable_llm)
                    hits, seen_hits = [], set()
                    for group in per_clause + [overall]:
                        for hit in group:
                            hid = (hit.get('evidence') or as_evidence(hit).to_dict())['evidence_id']
                            if hid not in seen_hits:
                                seen_hits.add(hid)
                                hits.append(hit)
                    record['retrieval_queries'] = len(per_clause) + 1
                    used = 0
                    for hit in hits:
                        ev = dict(hit.get('evidence') or as_evidence(hit).to_dict())
                        entry = self.kb.format_context([{'evidence':ev}], KB_REVIEW_EXCERPT_CHARS)
                        if used+len(entry)>evidence_budget:
                            continue
                        used += len(entry)
                        offered.append({'evidence':ev, **{k:v for k,v in hit.items() if k!='evidence'}})
                    record['retrieval_mode'] = sorted({r.get('retrieval_mode','keyword') for r in hits})
                except Exception as exc:
                    record['errors'].append('知识检索失败：'+str(exc)[:180])
            context = self.kb.format_context(offered, KB_REVIEW_EXCERPT_CHARS, numbered=True) if offered else ''
            relevant_rules = [r for r in self.rule_risks if r.clause_ref in ids or not r.clause_ref]
            rule_text = format_rule_risks_for_prompt(relevant_rules)[:400]
            candidate_count = len(offered)
            if self.enable_llm:
                def estimates():
                    values = {}
                    for kind in ('法律', '商业'):
                        prompt = build_review_prompt(kind, self.parsed['title'], len(active_clauses), text,
                            context, rule_text, self.contract_type, self.party_context, self.contract_overview)
                        messages = [{'role':'system','content':build_system_prompt(kind, self.party_context)},
                                    {'role':'user','content':prompt}]
                        values[kind] = sum(len(m['content'].encode('utf-8')) for m in messages)//2 + 128
                    return values
                prompt_estimates = estimates()
                target = LLM_NUM_CTX - LLM_MAX_TOKENS - 128
                while offered and max(prompt_estimates.values()) > target:
                    offered.pop()
                    context = self.kb.format_context(offered, KB_REVIEW_EXCERPT_CHARS, numbered=True) if offered else ''
                    prompt_estimates = estimates()
                if max(prompt_estimates.values()) > target and rule_text:
                    rule_text = ''
                    prompt_estimates = estimates()
                record['prompt_estimated_tokens'] = prompt_estimates
                if max(prompt_estimates.values()) > target:
                    raise ValueError('当前合同批次在移除证据摘要后仍超过模型上下文预算')
            record['evidence_candidates'] = candidate_count
            record['evidence_used'] = len(offered)
            record['evidence_dropped_for_context'] = candidate_count - len(offered)
            self._offered_evidence = {r['evidence']['evidence_id']:r['evidence'] for r in offered}
            self._offered_order = [r['evidence']['evidence_id'] for r in offered]
            for item in offered:
                ev = item['evidence']
                all_evidence[ev['evidence_id']] = ev
            detail = f'{len(offered)}条依据'
            if candidate_count > len(offered):
                detail += f'，为上下文预算移除{candidate_count-len(offered)}条'
            self._log_step(f'第{n}/{len(batches)}批检索', detail=detail)
            if not self.enable_llm:
                continue
            for kind, key in [('法律','legal'),('商业','commercial')]:
                if self.cancelled():
                    raise ReviewCancelled('审核已取消')
                self._log_step(f'第{n}/{len(batches)}批{kind}分析', 'running')
                raw = None
                try:
                    prompt = build_review_prompt(kind, self.parsed['title'], len(active_clauses), text,
                        context, rule_text, self.contract_type,
                        self.party_context, self.contract_overview)
                    raw = self.llm.chat([{'role':'system','content':build_system_prompt(kind, self.party_context)},
                                         {'role':'user','content':prompt}])
                    if getattr(self.llm, 'usage_log', None):
                        record[f'{key}_usage'] = dict(self.llm.usage_log[-1])
                    if key=='legal': self.raw_legal += f'\n批次{n}\n'+raw
                    else: self.raw_commercial += f'\n批次{n}\n'+raw
                    parse_error = None
                    try:
                        risks = self._parse_llm_response(raw, kind)
                    except ModelOutputError as exc:
                        parse_error = str(exc)
                        risks = []
                    item_stats = dict(getattr(self, '_last_parse_diagnostics', {}))
                    retriable = list(getattr(self, '_last_retriable_items', []))
                    if parse_error and not retriable:
                        raise ModelOutputError(parse_error)
                    if retriable and LLM_ITEM_RETRY_LIMIT:
                        item_stats['retry_attempted'] = len(retriable)
                        try:
                            retry_prompt = build_item_retry_prompt(kind, text, retriable)
                            retry_raw = self.llm.chat([
                                {'role':'system','content':build_system_prompt(kind, self.party_context)},
                                {'role':'user','content':retry_prompt},
                            ])
                            if getattr(self.llm, 'usage_log', None):
                                record[f'{key}_retry_usage'] = dict(self.llm.usage_log[-1])
                            if key == 'legal':
                                self.raw_legal += f'\n批次{n}引文纠正\n' + retry_raw
                            else:
                                self.raw_commercial += f'\n批次{n}引文纠正\n' + retry_raw
                            retry_risks = self._parse_llm_response(retry_raw, kind)
                            retry_stats = dict(getattr(self, '_last_parse_diagnostics', {}))
                            risks.extend(retry_risks)
                            recovered = min(len(retry_risks), len(retriable))
                            item_stats.update(
                                retry_received=retry_stats.get('received', 0),
                                retry_accepted=retry_stats.get('accepted', 0),
                                retry_rejected=retry_stats.get('rejected', 0),
                                recovered=recovered,
                                accepted=item_stats.get('accepted', 0) + recovered,
                                rejected=max(0, item_stats.get('rejected', 0) - recovered),
                            )
                        except Exception as retry_exc:
                            item_stats['retry_error'] = str(retry_exc)[:200]
                    self._last_parse_diagnostics = item_stats
                    if parse_error and not risks:
                        raise ModelOutputError(parse_error)
                    self._append_model_risks(risks)
                    record[f'{key}_items'] = item_stats
                    rejected = item_stats.get('rejected', 0)
                    record[key] = 'completed_with_rejections' if rejected else 'completed'
                    if rejected:
                        record.setdefault('warnings', []).append(
                            f"{kind}分析收到{item_stats['received']}项，采用{item_stats['accepted']}项，"
                            f"拒绝{rejected}项；详见{key}_items")
                    detail = f'发现{len(risks)}项' + (f'，隔离{rejected}项无效输出' if rejected else '')
                    self._log_step(f'第{n}/{len(batches)}批{kind}分析', 'done', detail)
                except Exception as exc:
                    record[key] = 'failed'
                    record['errors'].append(f'{kind}分析失败：{str(exc)[:200]}')
                    item_stats = dict(getattr(self, '_last_parse_diagnostics', {}))
                    if item_stats:
                        record[f'{key}_items'] = item_stats
                    # 保留原始输出片段，便于定位"模型没按格式返回"这类问题，不用重跑一遍
                    if raw:
                        record[f'{key}_raw_tail'] = raw[-400:]
                    self._log_step(f'第{n}/{len(batches)}批{kind}分析', 'error', str(exc)[:200])
        self.evidence_results = [{'evidence':e} for e in all_evidence.values()]
        self.legal_context = self.kb.format_context(self.evidence_results) if self.kb else ''
        self._dedupe_cross_route()
        self._correct_dingjin_violations()
        self._log_step('结果整理', detail='保留原文定位；法律适用和实质判断需人工复核')
        result = self._build_result()
        result['party_context'] = self.party_context
        return result

    def _parse_llm_response(self, raw, review_type):
        self._last_parse_diagnostics = {}
        self._last_retriable_items = []
        if not isinstance(raw,str) or not raw.strip():
            raise ModelOutputError('模型返回空内容')
        # Do not repair arbitrary prose or truncated JSON into a successful empty result.
        cleaned = re.sub(r' thinking[\s\S]*? response', '', raw).strip()
        blocks = re.findall(r'```(?:json)?\s*([\s\S]*?)\s*```', cleaned)
        candidate = blocks[-1].strip() if blocks else cleaned
        if not ((candidate.startswith('[') and candidate.endswith(']')) or
                (candidate.startswith('{') and candidate.endswith('}'))):
            raise ModelOutputError('模型返回的不是完整JSON')
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            # Minor quote/comma repairs only, after requiring balanced delimiters.
            import json_repair
            data = json_repair.loads(candidate)
        if isinstance(data,dict) and set(data)=={'risks'}:
            data = data['risks']
        if not isinstance(data,list):
            raise ModelOutputError('模型结果必须是风险数组')
        parsed_by_id = {c['clause_id']:c for c in (self.parsed or {}).get('clauses',[])}
        risks = []
        rejected = []
        for index, item in enumerate(data, 1):
            try:
                risks.append(self._parse_llm_item(item, review_type, parsed_by_id))
            except ModelOutputError as exc:
                reason = str(exc)
                rejected.append({'item': index,
                                 'title': item.get('title','')[:80] if isinstance(item,dict) else '',
                                 'reason': reason})
                if (isinstance(item, dict) and
                        reason in ('模型引用的合同原文无法定位', '模型引用了本批不存在的合同条款')):
                    self._last_retriable_items.append((dict(item), reason))
        self._last_parse_diagnostics = {'received':len(data), 'accepted':len(risks),
                                        'rejected':len(rejected), 'rejections':rejected}
        if data and not risks:
            raise ModelOutputError(f'模型返回{len(data)}项，但均未通过逐条校验')
        return risks

    def _append_model_risks(self, risks):
        """Use the item's declared nature for presentation, independent of prompt route."""
        for risk in risks:
            if risk.risk_type in ('legal', 'regulatory'):
                self.legal_risks.append(risk)
            else:
                self.commercial_risks.append(risk)

    def _parse_llm_item(self, item, review_type, parsed_by_id):
        if not isinstance(item,dict) or not all(isinstance(item.get(k),str) and item[k].strip()
            for k in ('title','description','suggestion','original_text','clause_id','level')):
            raise ModelOutputError('风险项缺少原文、条款ID或必要字段')
        level = LEVELS.get(item['level'].strip().lower())
        if level is None:
            raise ModelOutputError('模型风险等级无效')
        cid, quote = item['clause_id'].strip(), item['original_text'].strip()
        c = parsed_by_id.get(cid)
        if not c or (self._active_ids and cid not in self._active_ids):
            raise ModelOutputError('模型引用了本批不存在的合同条款')
        norm = lambda x: re.sub(r'\s+','',x)
        quote = re.sub(r'^\[c\d+\]\s*', '', quote)
        if not norm(quote) or norm(quote) not in norm(c['title']+'\n'+c['content']):
            raise ModelOutputError('模型引用的合同原文无法定位')
        claim = item['title'] + '；' + item['description']
        if cid in self._unselected_option_ids:
            raise ModelOutputError('模型引用了未选择的模板备选项；选择缺失只能定位到选择器条款')
        if cid in self._inactive_template_ids:
            raise ModelOutputError('模型引用了模板填写说明或未选择的备选项')
        if ('订金' in claim or '定金' in claim) and '订金' not in quote and '定金' not in quote:
            raise ModelOutputError('模型在未出现订金或定金的条款中虚构了该款项')
        if (re.search(r'有(?:诉讼)?管辖权的(?:人民)?法院', quote) and
                re.search(r'(?:法院|管辖).{0,12}(?:不明|不明确|未明确|未指定|缺少具体)|'
                          r'(?:不明|不明确|未明确|未指定|缺少具体).{0,12}(?:法院|管辖)', claim)):
            raise ModelOutputError('依法向有管辖权法院起诉不因未写具体法院而构成条款缺失')
        if (re.search(r'(?:合同一方|一方).{0,24}对方', quote) and
                not re.search(r'[甲乙]方', quote) and
                (re.search(r'(?:我方|[甲乙]方).{0,20}(?:提出|要求).{0,12}(?:变更|解除)', claim)
                 or ('我方' in claim and re.search(r'对方.{0,12}(?:接到|收到).{0,12}(?:通知|变更|解除)', claim)))):
            raise ModelOutputError('模型把对双方适用的条款错误归因于人工选择的一方')
        self._validate_stance(quote, claim)
        refs = item.get('evidence', [])
        if not isinstance(refs,list):
            refs = []
        valid, issues = [], []
        for ref in refs:
            key = self._resolve_evidence_ref(ref)
            if key and key not in valid:
                valid.append(key)
            else:
                issues.append(f'无效或本批未提供的证据引用：{str(ref)[:80]}')
        raw_confidence = item.get('confidence', .5)
        try:
            confidence_text = str(raw_confidence).strip().lower()
            confidence = CONFIDENCE[confidence_text] if confidence_text in CONFIDENCE else float(raw_confidence)
            if not math.isfinite(confidence): raise ValueError()
            confidence = min(1,max(0,confidence))
        except (ValueError,TypeError):
            confidence = .5
        risk_type = item.get('risk_type','legal' if review_type=='法律' else 'commercial')
        if risk_type not in ('legal','regulatory','commercial','company_policy','template_deviation'):
            risk_type = 'legal' if review_type=='法律' else 'commercial'
        # “违法/无效/法定资质”等仍是法律结论，不能通过把 risk_type 写成
        # commercial 来绕过法规证据校验。
        if risk_type not in ('legal', 'regulatory') and CATEGORICAL_LEGAL_CLAIM.search(claim):
            risk_type = 'legal'
            issues.append('该条包含确定性法律判断，已按法律风险执行证据校验')
        appropriate = {'legal':{'legal_kb'}, 'regulatory':{'legal_kb','industry_kb'},
                       'company_policy':{'company_kb'}, 'template_deviation':{'template_kb'}}
        if risk_type in appropriate:
            wrong_domain = [ref for ref in valid if self._offered_evidence[ref]['kb_type'] not in appropriate[risk_type]]
            if wrong_domain:
                issues.append('证据所属知识域与风险类型不匹配：' + '、'.join(wrong_domain))
            valid = [ref for ref in valid if ref not in wrong_domain]
        risk_tag = tag_by_risk(item['title'], item['description'], item.get('dimension', ''))
        if risk_tag == 'missing_clause':
            raise ModelOutputError('整份合同缺项只能由全文检查产生，不能由单个模型批次断言')
        if (risk_tag == 'acceptance_window' and '验收' not in quote and
                re.search(r'未约定|缺少|未明确|未发现', claim)):
            raise ModelOutputError('当前引文不含验收约定，不能据此断言整份合同缺少验收条款')
        if (re.search(r'合同(?:全文|中).{0,16}(?:未检出|未发现|未约定|缺少).{0,12}验收', claim)
                and '验收' not in quote):
            raise ModelOutputError('单个条款不能支撑整份合同缺少验收约定的结论')
        if risk_tag == 'penalty_rate':
            direct = ('civil_585', 'interp_2023_13_65', 'interp_2023_13_66')
            unrelated = [ref for ref in valid if not any(code in ref for code in direct)]
            if unrelated:
                issues.append('高额违约金结论移除了不直接适用的法条引用：' + '、'.join(unrelated))
                valid = [ref for ref in valid if ref not in unrelated]
        civil_858_misstatement = re.search(
            r'(?:违反|冲突|不符合|不一致).{0,24}(?:八百五十八|858)|'
            r'(?:八百五十八|858).{0,24}(?:违反|冲突|不符合|不一致)', claim)
        if 'legal_kb:civil_858' in valid and civil_858_misstatement:
            raise ModelOutputError('模型错误地把允许当事人约定的研发风险分担条款判断为违反第八百五十八条')
        related = []
        if risk_type in ('legal', 'regulatory'):
            valid, related, evidence_status, support_issues = split_claim_evidence(
                claim, quote, risk_tag, self._offered_evidence, valid)
            issues.extend(support_issues)
            if evidence_status == 'unsupported' and CATEGORICAL_LEGAL_CLAIM.search(claim):
                raise ModelOutputError('模型给出了缺少直接法规支撑的确定性法律结论')
            if evidence_status != 'claim_aligned' and level == RiskLevel.HIGH:
                level = RiskLevel.MEDIUM
                issues.append('缺少直接法规支撑，模型风险等级已降为中风险待核查')
        elif valid:
            related = list(valid)
            valid = []
            evidence_status = 'topic_related'
            issues.append('商业风险引用的法规仅作为相关材料，不显示为直接法律依据')
        else:
            evidence_status = 'not_applicable' if risk_type == 'commercial' else 'needs_review'
        bases = [self._offered_evidence[ref].get('citation') or self._offered_evidence[ref]['title'] for ref in valid]
        self._risk_counter += 1
        return RiskItem(f'llm_{self._risk_counter:04d}', level,
            item.get('dimension') if item.get('dimension') in SEVEN_DIMENSIONS else '合法性合规性' if review_type=='法律' else '商业风险',
            item['title'], item['description'], clause_ref=cid, original_text=quote,
            legal_basis='；'.join(bases) or None, suggestion=item['suggestion'], source='llm_'+review_type,
            confidence=confidence, risk_type=risk_type, evidence=valid, review_status='needs_review',
            evidence_status=evidence_status, validation_issues=issues, related_evidence=related,
            commercial_impact=item.get('commercial_impact') if isinstance(item.get('commercial_impact'),str) else None)

    @staticmethod
    def _find_unselected_option_ids(clauses):
        """识别“按以下第____项执行”后紧邻的编号备选项。"""
        result = {c.get('clause_id') for c in clauses if c.get('template_status') == 'unselected_option'}
        for index, clause in enumerate(clauses):
            selector = (clause.get('title', '') + '\n' + clause.get('content', ''))
            if not re.search(r'(?:以下|下列|按).{0,16}第?[_＿]{2,}项', selector):
                continue
            for option in clauses[index + 1:]:
                text = (option.get('title', '') or option.get('content', '')).lstrip()
                if re.match(r'[（(]?[一二三四五六七八九十\d]+[)）、.．]', text):
                    result.add(option.get('clause_id'))
                elif result:
                    break
        return {cid for cid in result if cid}

    def _validate_stance(self, quote, claim):
        """隔离与用户人工立场存在明确文字矛盾的商业结论。"""
        if self.party_context.get('our_side') not in ('甲', '乙'):
            return
        our = self.party_context['our_side'] + '方'
        other = ('乙' if self.party_context['our_side'] == '甲' else '甲') + '方'
        if '对我方有利' in claim and '对我方不利' not in claim:
            raise ModelOutputError('模型把对我方有利的条款列为商业风险')
        other_liability = (
            re.search(rf'(?<!向)(?<!给){other}.{{0,24}}(?:承担|负责|支付).{{0,16}}(?:损失|赔偿|违约金|费用)', quote)
            or re.search(rf'(?:损失|赔偿|违约金|费用).{{0,12}}(?:由)?{other}.{{0,12}}(?:承担|负责|支付)', quote)
        )
        our_liability = (
            re.search(rf'(?<!向)(?<!给){our}.{{0,24}}(?:承担|负责|支付).{{0,16}}(?:损失|赔偿|违约金|费用)', quote)
            or re.search(rf'(?:损失|赔偿|违约金|费用).{{0,12}}(?:由)?{our}.{{0,12}}(?:承担|负责|支付)', quote)
        )
        contradicts = ('对我方不利' in claim or
                       bool(re.search(r'我方.{0,18}(?:无法|难以).{0,12}(?:追责|索赔|维权)', claim)))
        limiting_words = any(word in quote for word in ('不承担', '仅承担', '最高', '上限', '免责'))
        if other_liability and not our_liability and contradicts and not limiting_words:
            raise ModelOutputError('模型把对方承担损失的条款反向判断为对我方不利')
        if (self.party_context.get('we_pay') is True
                and re.search(r'总价.{0,8}(?:不得|不予).{0,8}(?:变更|调整)', quote)
                and bool(re.search(rf'(?:{our}|我方).{{0,16}}(?:承担|增加).{{0,12}}(?:成本|费用)', claim))):
            raise ModelOutputError('模型把固定总价反向判断为付款方承担成本上涨')
        if (self.party_context.get('we_pay') is True
                and re.search(r'验收合格.{0,80}(?:甲方|我方).{0,12}(?:支付|付款)', quote)
                and re.search(r'付款延迟.{0,20}(?:我方|甲方).{0,12}(?:不利|资金安排)', claim)):
            raise ModelOutputError('模型把验收后付款对付款方的制约方向判断反了')
        if (self.party_context.get('our_side') == '甲'
                and '甲方有权' in quote and re.search(r'乙方应.{0,12}(?:认可|负责)', quote)
                and re.search(r'(?:责任全部由甲方承担|增加甲方责任|甲方承担全部)', claim)):
            raise ModelOutputError('模型把甲方权利和乙方责任反向判断为甲方承担全部责任')
        if (other_liability and not our_liability and
                re.search(r'看似保护(?:甲方|乙方|我方)', claim)):
            raise ModelOutputError('模型把明确由对方承担损失的有利安排转写成我方风险')

    def _resolve_evidence_ref(self, ref):
        """证据引用兼容两种写法：上下文里的 [1][2]… 序号，或完整 evidence_id。"""
        if isinstance(ref, bool) or ref is None:
            return None
        if isinstance(ref, int) or (isinstance(ref, str) and ref.strip().isdigit()):
            index = int(str(ref).strip())
            order = getattr(self, '_offered_order', []) or []
            return order[index-1] if 1 <= index <= len(order) else None
        if isinstance(ref, str):
            ref = ref.strip()
            if ref in self._offered_evidence:
                return ref
            # 模型可能只回 id 的后半段（去掉 kb_type 前缀）
            for key in self._offered_evidence:
                if ref and (ref in key or key.endswith(':' + ref)):
                    return key
        return None

    def _dedupe_cross_route(self):
        """合并三条审核路线的同一问题，同时保留不同条款、数值和权利义务。"""
        self._locate_rule_risks()
        kept, route_for_key = {}, {}
        route_lists = [('rule', self.rule_risks), ('legal', self.legal_risks),
                       ('commercial', self.commercial_risks)]
        norm = lambda x: re.sub(r'\W+', '', x or '')
        for route, items in route_lists:
            for risk in items:
                risk.risk_type_tag = tag_by_risk(
                    risk.title, '\n'.join(filter(None, [risk.description, risk.original_text])), risk.dimension)
                if risk.risk_type_tag in SAFE_DEDUPE_TAGS:
                    location = ('document' if risk.risk_type_tag in {'blank_fields', 'missing_clause', 'contract_scope'}
                                else risk.clause_ref or norm(risk.original_text) or 'document')
                    fact = _dedupe_fact(risk.risk_type_tag, risk)
                    if risk.risk_type_tag in {'penalty_rate', 'prepay_ratio'}:
                        rate_text = normalize_rate_notation(risk.original_text or
                            ' '.join(filter(None, [risk.title, risk.description])))
                        fact = '|'.join(dict.fromkeys(re.findall(r'\d+(?:\.\d+)?\s*%', rate_text)))
                    key = ('tag', risk.risk_type_tag, location, fact)
                elif risk.clause_ref and risk.original_text:
                    key = ('exact', risk.clause_ref, norm(risk.original_text), norm(risk.title))
                else:
                    key = ('unique', id(risk))
                if key in kept:
                    kept[key] = _merge_risk_group(_pick_survivor([kept[key], risk]), [kept[key], risk])
                    if route_for_key[key] != 'rule' and route == 'rule':
                        route_for_key[key] = 'rule'
                    elif route_for_key[key] == 'commercial' and route == 'legal':
                        route_for_key[key] = 'legal'
                else:
                    kept[key], route_for_key[key] = risk, route
        self.rule_risks = [risk for key,risk in kept.items() if route_for_key[key]=='rule']
        self.legal_risks = [risk for key,risk in kept.items() if route_for_key[key]=='legal']
        self.commercial_risks = [risk for key,risk in kept.items() if route_for_key[key]=='commercial']

    def _locate_rule_risks(self):
        clauses = (self.parsed or {}).get('clauses', [])
        norm = lambda x: re.sub(r'\s+', '', x or '')
        for risk in self.rule_risks:
            if risk.clause_ref or not risk.original_text:
                continue
            matches = [c['clause_id'] for c in clauses
                       if norm(risk.original_text) in norm(c.get('title','')+'\n'+c.get('content',''))]
            if len(matches) == 1:
                risk.clause_ref = matches[0]

    def _correct_dingjin_violations(self):
        """P0-3 兜底校正：LLM 路（含合并结果）即使 prompt 已约束，仍可能把『订金』当『定金』审——
        断言其超 20% 上限、援引第五百八十六条/五百八十七条定金法条。这类错误结论会因“取最高等级”
        合并盖过规则引擎的正确结论，故在此强制按规则引擎口径拉回，保证展示给用户的版本符合 P0-3。"""
        import re as _re
        for lst in (self.rule_risks, self.legal_risks, self.commercial_risks):
            for r in lst:
                if r.risk_type_tag != "dingjin":
                    continue
                cited_dingjin_law = _re.search(r'五百八十[六七]|(?:5|五)8[67]|586|587', r.legal_basis or "")
                title_asserts_excess = _re.search(r'超过|超出|超标|上限|远超|无效', r.title or "")
                if r.evidence:
                    r.evidence = []
                    r.legal_basis = None
                    r.evidence_status = 'needs_review'
                    r.validation_issues = list(r.validation_issues or []) + ['订金性质提示不沿用模型提供的非直接法条引用']
                if not (cited_dingjin_law or title_asserts_excess):
                    continue  # 该路已符合 P0-3，不动
                corr = check_dingjin(r.original_text or "")
                src = corr[0] if corr else None
                r.title = src.title if src else "「订金」与「定金」概念混用、性质约定不清"
                r.description = (src.description + (f"\n原文：{r.original_text[:120]}" if src and r.original_text else "")
                                 if src else "合同使用『订金』（预付款性质），却被错误地按定金罚则与20%上限审查，结论不成立。")
                r.legal_basis = None
                r.suggestion = src.suggestion if src else r.suggestion
                if src:
                    r.level = src.level  # 规则为中风险，拉低 LLM 误标的高风险
                r.review_status = "needs_review"

    def _build_result(self):
        all_risks = self.rule_risks+self.legal_risks+self.commercial_risks
        ids = [c['clause_id'] for c in self.parsed['clauses']]
        reviewable_ids = [cid for cid in ids if cid not in self._inactive_template_ids]
        covered = {}
        for route in ('legal','commercial'):
            covered[route] = [cid for cid in reviewable_ids if self.enable_llm and all(
                b[route] in ('completed', 'completed_with_rejections')
                for b in self.batch_records if cid in b['clause_ids'])]
        errors = [e for b in self.batch_records for e in b['errors']]
        warnings = [w for b in self.batch_records for w in b.get('warnings', [])]
        complete = (self.enable_llm and
                    all(len(covered[k]) == len(reviewable_ids) for k in covered) and not errors)
        completeness = 'complete' if complete else 'partial' if self.enable_llm else 'rules_only'
        evidence = []
        for r in self.evidence_results:
            e = dict(r['evidence'])
            e['source_type_label'] = source_type_label(e['kb_type'])
            evidence.append(e)
        root = Path(__file__).resolve().parents[1]
        def digest(paths):
            return hashlib.sha256(b''.join((root/p).read_bytes() for p in paths)).hexdigest()[:16]
        return {'schema_version':4, 'contract_title':self.parsed['title'], 'contract_type':self.contract_type,
                'clause_count':len(ids), 'clauses':self.parsed['clauses'], 'steps':self.steps,
                'review_mode':'full' if self.enable_llm else 'rules_only', 'completeness':completeness,
                'document_checklist':self.document_checklist,
                'review_errors':errors, 'review_warnings':warnings,
                'document_warnings':self.parsed.get('warnings',[]),
                'coverage':{'total_clauses':len(ids), 'llm_applicable_clauses':reviewable_ids,
                            'excluded_template_clauses':sorted(self._inactive_template_ids),
                            'rule_checked':reviewable_ids,
                            'legal_checked':covered['legal'], 'commercial_checked':covered['commercial'],
                            'unreviewed_by_llm':[cid for cid in reviewable_ids
                                if cid not in covered['legal'] or cid not in covered['commercial']]},
                'batches':self.batch_records,
                'summary':{'total':len(all_risks), **{key:sum(r.level==level for r in all_risks)
                           for key,level in [('high',RiskLevel.HIGH),('medium',RiskLevel.MEDIUM),('low',RiskLevel.LOW),('info',RiskLevel.INFO)]}},
                'rule_risks':[risk_to_dict(r) for r in self.rule_risks],
                'legal_risks':[risk_to_dict(r) for r in self.legal_risks],
                'commercial_risks':[risk_to_dict(r) for r in self.commercial_risks],
                'evidence':evidence, 'legal_context':self.legal_context,
                'raw_llm_legal':self.raw_legal, 'raw_llm_commercial':self.raw_commercial,
                'provenance':{'model':getattr(self.llm,'model','test'), 'reviewed_at':datetime.now().isoformat(timespec='seconds'),
                    'input_text_sha256':hashlib.sha256(self.parsed['full_text'].encode()).hexdigest(),
                    'rules_version':digest(['core/rules.py','core/clause_checks.py']),
                    'prompt_version':digest(['agent/prompts.py']), 'pipeline_version':digest(['agent/pipeline.py']),
                    'knowledge_version':hashlib.sha256(json.dumps(self.kb.metadata_by_domain if self.kb else {},ensure_ascii=False,sort_keys=True).encode()).hexdigest()[:16],
                    'elapsed_seconds':round(time.time()-self._started_at,2) if self._started_at else None,
                    'usage':self.llm.usage_summary(start=self._usage_start) if hasattr(self.llm,'usage_summary') else None}}


def _pick_survivor(group):
    deterministic = [r for r in group if r.source == 'rule' or r.source.startswith('规则+')]
    if deterministic:
        return max(deterministic, key=lambda r: r.confidence or 0)
    return max(group, key=lambda r: (bool(r.evidence), bool(r.related_evidence), r.confidence or 0))


def _merge_risk_group(survivor, group):
    order = {RiskLevel.INFO:0, RiskLevel.LOW:1, RiskLevel.MEDIUM:2, RiskLevel.HIGH:3}
    survivor.level = max((r.level for r in group), key=order.get)
    for field in ('legal_basis','commercial_impact'):
        values = list(dict.fromkeys(getattr(r,field) for r in group if getattr(r,field)))
        setattr(survivor,field,'；'.join(values) or None)
    for field in ('evidence','related_evidence','related_clause_ids','validation_issues'):
        merged = []
        for r in group:
            for value in getattr(r,field) or []:
                if value not in merged: merged.append(value)
        setattr(survivor,field,merged)
    survivor.related_evidence = [ref for ref in survivor.related_evidence
                                 if ref not in survivor.evidence]
    survivor.review_status = 'needs_review'
    if survivor.evidence:
        survivor.evidence_status = 'claim_aligned'
    elif survivor.related_evidence:
        survivor.evidence_status = 'topic_related'
    elif survivor.risk_type == 'commercial':
        survivor.evidence_status = 'not_applicable'
    else:
        survivor.evidence_status = 'needs_review'
    sources = {r.source for r in group}
    survivor.source = ('规则+模型合并' if any(s == 'rule' or s.startswith('规则+') for s in sources)
                       else 'llm_合并')
    return survivor


def _dedupe_fact(tag, risk):
    primary = ' '.join(filter(None, [risk.title, risk.description]))
    fallback = risk.original_text or ''
    facets = {
        'confidentiality': [('scope', ('范围', '定义', '过宽')), ('duration', ('期限', '永久', '终止后')),
                            ('return', ('返还', '销毁', '删除')), ('breach', ('违约', '赔偿')),
                            ('access', ('人员', '查阅', '权限', '披露'))],
        'ip_ownership': [('clearance', ('检索', '自由实施', 'FTO', '保护范围')),
                         ('ownership', ('归属', '所有权', '申请权')), ('license', ('许可', '使用权')),
                         ('infringement', ('侵权', '第三方权利')), ('background', ('背景知识产权', '原有'))],
        'termination': [('right', ('解除权', '终止权', '单方')), ('notice', ('通知', '送达')),
                        ('effect', ('结算', '返还', '终止后'))],
        'liability_exclusion': [('product', ('质量', '故障', '产品')), ('injury', ('人身', '伤亡')),
                                ('fault', ('故意', '重大过失')), ('cap', ('限额', '上限')),
                                ('consequential', ('间接损失', '可得利益'))],
        'quality': [('standard', ('标准', '指标')), ('warranty', ('质保', '保修')),
                    ('remedy', ('修理', '更换', '退货'))],
        'payment_selection': [('choice', ('选择', '选项', '方式一', '方式二', '自拟'))],
        'price_adjustment': [('fixed', ('固定', '不得变更', '不得调整', '调价'))],
        'oral_agreement': [('form', ('口头', '书面', '举证'))],
        'development_failure': [('failure', ('研发失败', '开发失败', '技术困难', '实际工作量'))],
    }
    # For IP findings the quoted clause is the more reliable discriminator.  Model
    # descriptions often say "责任归属不明" about an infringement warranty; reading
    # that phrase first incorrectly splits two reports about the same quoted duty
    # into ownership and infringement findings.
    texts = (fallback, primary) if tag == 'ip_ownership' else (primary, fallback)
    for text in texts:
        for facet, words in facets.get(tag, []):
            if any(word in text for word in words):
                return facet
    return 'general'


def print_full_report(result):
    print(f"合同：{result['contract_title']}｜完整性：{result.get('completeness','unknown')}")
    print(json.dumps(result['summary'], ensure_ascii=False))
    for r in result['rule_risks']+result['legal_risks']+result['commercial_risks']:
        print(f"[{r['level']}] [{r.get('clause_id') or '全文'}] {r['title']}\n  {r['description']}\n  建议：{r.get('suggestion','')}")
    for error in result.get('review_errors',[]):
        print('未完成：'+error)
