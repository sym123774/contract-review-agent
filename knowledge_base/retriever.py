"""Routed retrieval with current metadata, date checks and a network-free lexical fallback."""
import hashlib
import json
import math
import os
import re
from collections import Counter
from datetime import date
import numpy as np
try:
    import faiss
    HAS_FAISS = True
except ImportError:
    faiss = None
    HAS_FAISS = False
from config import KB_DIR, RETRIEVE_TOP_K, RETRIEVE_SCORE_THRESHOLD, EMBEDDING_MODEL, KB_REVIEW_DOMAINS
from core.llm_client import LLMClient
from knowledge_base.models import KBType, RetrievalRequest, as_evidence
from knowledge_base.registry import get_records_by_domain
from knowledge_base.router import domains_for

DOMAIN_LABELS = {'legal_kb':'法律法规依据', 'industry_kb':'医疗器械行业依据',
                 'company_kb':'公司政策与审核规则', 'template_kb':'标准合同与条款参照'}
PRIORITY = {'statute':0, 'judicial_interpretation':1, 'regulation':2,
            'company_redline':3, 'company_policy':4, 'standard_clause':5, 'reference':6}

MEDICAL_SIGNALS = ('医疗器械', '医疗设备', '注册证', '备案人', '药品监督管理', '临床试验器械')
DATA_PRIVACY_SIGNALS = ('个人信息', '个人数据', '敏感信息', '隐私', '数据出境', '境外提供',
                        '委托处理', '数据处理者', '患者数据', '临床数据', '人脸', '身份证')
SMALL_BUSINESS_SIGNALS = ('中小企业', '小微企业', '中型企业', '小型企业', '微型企业')
TRANSPORT_SIGNALS = ('运输合同', '承运人', '托运人', '收货人', '运费', '货运', '客运', '多式联运')
SECURITY_SIGNALS = ('担保', '保证人', '保证期间', '抵押', '质押', '留置', '所有权保留',
                    '融资租赁', '保理', '应收账款质押', '增信', '差额补足', '保证金账户')
SALE_SIGNALS = ('买卖合同', '采购合同', '购销合同', '出卖人', '买受人', '供方', '采购方',
                '货物', '交付', '收货', '验收', '质量异议', '质量保证金', '价款', '发票')
ELECTRONIC_SIGNATURE_SIGNALS = ('电子合同', '电子签名', '电子签章', '数字签名', '数据电文',
                                '在线签署', '邮件签署', '可靠电子签名', '电子认证')
ARBITRATION_SIGNALS = ('仲裁', '仲裁协议', '仲裁条款', '仲裁机构', '仲裁委员会', '仲裁地',
                       '裁决', '或裁或审', '争议解决')
TECHNOLOGY_SCOPE_SIGNALS = {
    'technology_development': ('技术开发', '研究开发', '研发成果', '开发失败'),
    'technology_transfer_license': ('技术转让', '技术许可', '专利许可', '许可使用'),
    'technology_consulting_service': ('技术咨询', '技术服务', '技术顾问'),
}


def _tokens(text):
    out = []
    for run in re.findall(r'[\u4e00-\u9fff]+|[a-zA-Z0-9_]+', text.lower()):
        if re.fullmatch(r'[a-z0-9_]+', run):
            out.append(run)
        elif len(run) == 1:
            out.append(run)
        else:
            out.extend(run[i:i+n] for n in (2, 3) for i in range(len(run)-n+1))
    return out


def _fingerprint(records):
    payload = [(r['knowledge_id'], r['title'], r['content']) for r in records]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()


class KnowledgeBase:
    def __init__(self, llm_client=None, **kwargs):
        self.llm = llm_client or LLMClient()
        self.indexes, self.metadata_by_domain, self.memory_records = {}, {}, {}
        self.index, self.metadata, self._loaded = None, [], False
        self.warnings = []
        self._lex_cache = {}

    def _paths(self, domain):
        return os.path.join(KB_DIR, f'faiss_{domain}.bin'), os.path.join(KB_DIR, f'metadata_{domain}.json')

    def load(self):
        # Current source controls publication/applicability; disk may contain obsolete metadata.
        self.metadata_by_domain = get_records_by_domain()
        self.memory_records = self.metadata_by_domain
        self.indexes = {}
        self.warnings = []
        for domain, records in self.metadata_by_domain.items():
            ip, mp = self._paths(domain)
            manifest_path = mp + '.manifest.json'
            if not HAS_FAISS or not all(os.path.exists(p) for p in (ip, mp, manifest_path)):
                if domain in KB_REVIEW_DOMAINS:
                    reason = '缺少faiss运行库' if not HAS_FAISS else '索引文件或版本清单缺失'
                    self.warnings.append(f'{domain}向量索引不可用（{reason}），当前使用关键词检索。')
                continue
            try:
                manifest = json.load(open(manifest_path, encoding='utf-8'))
                if manifest.get('embedding_model') != self.llm.embedding_model or manifest.get('fingerprint') != _fingerprint(records):
                    self.warnings.append(f'{domain}索引版本与当前数据/模型不一致，使用关键词检索。')
                    continue
                index = faiss.read_index(ip)
                if index.ntotal != len(records):
                    raise ValueError('索引记录数不一致')
                self.indexes[domain] = index
            except Exception as exc:
                self.warnings.append(f'{domain}索引未加载：{type(exc).__name__}')
        self._loaded = bool(self.metadata_by_domain)
        self._sync_legacy_view()
        return self._loaded

    def _sync_legacy_view(self):
        self.index = self.indexes.get('legal_kb')
        self.metadata = self.metadata_by_domain.get('legal_kb', [])

    def build(self, force=False, kb_types=None, backend='faiss'):
        if backend != 'faiss':
            raise ValueError('当前稳定审核链路仅支持FAISS；Qdrant尚未完成端到端验证。')
        self.load()
        if not HAS_FAISS:
            return self.is_ready()
        wanted = kb_types or list(self.metadata_by_domain)
        for domain in wanted:
            if not force and domain in self.indexes:
                continue
            records = self.metadata_by_domain.get(domain, [])
            if not records:
                raise ValueError(f'未知知识域：{domain}')
            # Do not publish a partial index when any embedding fails.
            vectors = []
            for number, record in enumerate(records, 1):
                vectors.append(self.llm.embed(record['title'] + '\n' + record['content']))
                if number == 1 or number % 25 == 0 or number == len(records):
                    print(f'  {domain} 向量化 {number}/{len(records)}')
            matrix = np.asarray(vectors, dtype=np.float32)
            if matrix.ndim != 2 or not matrix.shape[1] or not np.isfinite(matrix).all():
                raise ValueError('向量服务返回了无效数据')
            faiss.normalize_L2(matrix)
            index = faiss.IndexFlatIP(matrix.shape[1])
            index.add(matrix)
            ip, mp = self._paths(domain)
            os.makedirs(KB_DIR, exist_ok=True)
            faiss.write_index(index, ip + '.tmp')
            with open(mp + '.tmp', 'w', encoding='utf-8') as f:
                json.dump(records, f, ensure_ascii=False)
            manifest = {'embedding_model': self.llm.embedding_model, 'dimension': matrix.shape[1],
                        'fingerprint': _fingerprint(records), 'built_at': date.today().isoformat()}
            with open(mp + '.manifest.json.tmp', 'w', encoding='utf-8') as f:
                json.dump(manifest, f)
            os.replace(ip + '.tmp', ip)
            os.replace(mp + '.tmp', mp)
            os.replace(mp + '.manifest.json.tmp', mp + '.manifest.json')
            self.indexes[domain] = index
        self._sync_legacy_view()
        return True

    @staticmethod
    def _matches(record, filters=None):
        filters = filters or {}
        if record.get('status') != filters.get('status', 'published'):
            return False
        if record.get('review_status') != filters.get('review_status', 'published'):
            return False
        at = filters.get('effective_at') or date.today().isoformat()
        try:
            check_date = date.fromisoformat(at)
            start = record.get('effective_from') or record.get('effective_date')
            end = record.get('effective_to')
            if start and date.fromisoformat(start) > check_date:
                return False
            if end and date.fromisoformat(end) < check_date:
                return False
        except (ValueError, TypeError):
            return False
        applicable = record.get('contract_types') or []
        ct = filters.get('contract_type')
        if ct and applicable and ct not in applicable and 'general' not in applicable:
            return False
        dimensions = record.get('clause_dimensions') or []
        dim = filters.get('clause_dimension')
        if dim and dimensions and dim not in dimensions:
            return False
        return True

    @staticmethod
    def _scope_matches(record, query, filters=None):
        """对 legal_kb 内部专属法规做事实门控，避免仅凭词面相似误用。"""
        scope = record.get('applicability_scope', 'general')
        if scope == 'general':
            return True
        filters = filters or {}
        contract_type = filters.get('contract_type') or ''
        text = str(query or '')
        if scope == 'medical_device':
            return contract_type.startswith('medical_device_') or any(k in text for k in MEDICAL_SIGNALS)
        if scope == 'data_privacy':
            return any(k in text for k in DATA_PRIVACY_SIGNALS)
        if scope == 'small_business_payment':
            return any(k in text for k in SMALL_BUSINESS_SIGNALS)
        if scope == 'transport':
            return contract_type == 'transport' or any(k in text for k in TRANSPORT_SIGNALS)
        if scope == 'security':
            return any(k in text for k in SECURITY_SIGNALS)
        if scope == 'sale':
            return any(k in text for k in SALE_SIGNALS)
        if scope == 'electronic_signature':
            return any(k in text for k in ELECTRONIC_SIGNATURE_SIGNALS)
        if scope == 'arbitration':
            return any(k in text for k in ARBITRATION_SIGNALS)
        if scope == 'technology_general':
            return contract_type.startswith('technology_')
        if scope in TECHNOLOGY_SCOPE_SIGNALS:
            return contract_type == scope or any(k in text for k in TECHNOLOGY_SCOPE_SIGNALS[scope])
        return False

    def _lexical(self, query, domains, filters):
        terms = Counter(_tokens(query))
        results = []
        for domain in domains:
            records = self.metadata_by_domain.get(domain, [])
            key = (domain, id(records), len(records))
            if key not in self._lex_cache:
                vectors, freq = [], Counter()
                for r in records:
                    counts = Counter(_tokens(r['title']) * 3 + _tokens(' '.join(r.get('tags', []))) * 2 + _tokens(r['content']))
                    vectors.append(counts)
                    freq.update(counts.keys())
                avg = sum(sum(v.values()) for v in vectors) / max(len(vectors), 1)
                self._lex_cache[key] = vectors, freq, avg
            vectors, freq, avg = self._lex_cache[key]
            for record, counts in zip(records, vectors):
                if not self._matches(record, filters) or not self._scope_matches(record, query, filters):
                    continue
                score = 0.0
                length = sum(counts.values())
                for term in terms:
                    tf = counts.get(term, 0)
                    if tf:
                        idf = math.log(1 + (len(records) - freq[term] + .5) / (freq[term] + .5))
                        score += idf * tf * 2.2 / (tf + 1.2 * (.25 + .75 * length / max(avg, 1)))
                if score:
                    results.append(self._result(record, score / (score + 8), 'keyword'))
        return results

    @staticmethod
    def _result(record, score, mode):
        validity = 'valid' if record.get('effective_from') or record.get('effective_date') else 'date_unverified'
        return {'chunk': record, 'score': float(score), 'validity': validity, 'retrieval_mode': mode}

    def search(self, query, top_k=None, threshold=None, kb_type=None, filters=None,
               use_vector=True, query_vector=None):
        if not self._loaded or not str(query).strip():
            return []
        if kb_type and kb_type not in self.metadata_by_domain:
            return []
        domains = [kb_type] if kb_type else list(self.metadata_by_domain)
        limit = top_k if top_k is not None else RETRIEVE_TOP_K
        if limit < 1:
            return []
        results = self._lexical(query, domains, filters)
        ranked = {r['chunk']['knowledge_id']: r for r in results}
        if use_vector and HAS_FAISS and any(d in self.indexes for d in domains):
            try:
                v = query_vector if query_vector is not None else self.llm.embed(query)
                matrix = np.asarray([v], dtype=np.float32)
                faiss.normalize_L2(matrix)
                for domain in domains:
                    index = self.indexes.get(domain)
                    if index is None or index.d != matrix.shape[1]:
                        continue
                    records = self.metadata_by_domain[domain]
                    scores, indices = index.search(matrix, len(records))
                    for score, idx in zip(scores[0], indices[0]):
                        if (idx < 0 or score < RETRIEVE_SCORE_THRESHOLD or
                                not self._matches(records[idx], filters) or
                                not self._scope_matches(records[idx], query, filters)):
                            continue
                        record = records[idx]
                        rid = record['knowledge_id']
                        lexical = ranked.get(rid, {}).get('score', 0)
                        ranked[rid] = self._result(record, .65 * float(score) + .35 * lexical, 'hybrid')
            except Exception:
                # Lexical evidence remains available. Report actual mode on each hit.
                pass
        cutoff = 0 if threshold is None else threshold
        ordered = sorted((r for r in ranked.values() if r['score'] >= cutoff),
                         key=lambda r: (-r['score'], r['chunk']['knowledge_id']))
        return ordered[:limit]

    def retrieve(self, request, use_vector=True):
        # 审核链路的知识域白名单：默认 config.KB_REVIEW_DOMAINS（仅法规域），
        # 调用方显式给 required_kb_types 时以其为准（离线脚本/测试用）。
        domains = domains_for(request.contract_type, request.clause_dimension,
                              allowed=request.required_kb_types or KB_REVIEW_DOMAINS)
        filters = {'contract_type': request.contract_type, 'clause_dimension': request.clause_dimension,
                   'effective_at': request.effective_at}
        vector = None
        if use_vector and any(d in self.indexes for d in domains):
            try:
                vector = self.llm.embed(request.query)
            except Exception:
                use_vector = False
        results = []
        for d in domains:
            for r in self.search(request.query, request.top_k_per_kb, request.threshold, d, filters,
                                 use_vector=use_vector, query_vector=vector):
                r['evidence'] = as_evidence(r).to_dict()
                results.append(r)
        return sorted(results, key=lambda r: (-r['score'], PRIORITY.get(r['chunk'].get('authority_level'), 9)))

    def search_for_clause(self, clause_text, top_k=3, contract_type=None, clause_dimension=None):
        return self.retrieve(RetrievalRequest(clause_text, contract_type=contract_type,
                            clause_dimension=clause_dimension, top_k_per_kb=top_k))

    def format_context(self, results, max_excerpt=None, numbered=False):
        """渲染证据上下文。numbered=True 时额外给出 [1][2]… 序号，
        模型可以直接引用序号，不必逐字抄写很长的 evidence_id。"""
        parts = []
        for index, r in enumerate(results, 1):
            e = r.get('evidence') or as_evidence(r).to_dict()
            excerpt = e['excerpt']
            if max_excerpt and len(excerpt) > max_excerpt:
                excerpt = excerpt[:max_excerpt] + '…（节选，完整原文见结果）'
            tag = f"[{index}] {e['evidence_id']}" if numbered else f"[{e['evidence_id']}]"
            parts.append(f"{tag}｜{DOMAIN_LABELS.get(e['kb_type'], e['kb_type'])}｜{e['title']}\n"
                         f"引用：{e.get('citation') or e['title']}\n原文：{excerpt}")
        return '\n\n'.join(parts)

    def is_ready(self):
        return self._loaded and bool(self.metadata_by_domain)


def build_knowledge_base(force=False):
    return KnowledgeBase().build(force=force)

