"""Shared models for the layered contract-review knowledge center."""
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional
from enum import Enum

_ARTICLE_RE = re.compile(r"第[一二三四五六七八九十百千万零OoO0-9]+条")


def _extract_source_and_article(title: str, citation: str = None):
    """从 title 或 citation 中正则提取 source 和 article_or_clause。
    title 格式如: '民法典第五百八十五条 违约金' → source='民法典', article='第五百八十五条'
    citation 格式如: '《医疗器械监督管理条例》第三十四条' → source='医疗器械监督管理条例', article='第三十四条'
    """
    source = None
    article = None
    for text in (title, citation):
        if not text:
            continue
        match = _ARTICLE_RE.search(text)
        if match:
            if not article:
                article = match.group()
            prefix = text[:match.start()].strip()
            if prefix and not source:
                if prefix.startswith("《") and prefix.endswith("》"):
                    prefix = prefix[1:-1]
                source = prefix
            break
    if not source and citation:
        book_match = re.search(r"《(.+?)》", citation)
        if book_match:
            source = book_match.group(1)
    return source, article

class KBType(str, Enum):
    LEGAL = "legal_kb"
    INDUSTRY = "industry_kb"
    COMPANY = "company_kb"
    TEMPLATE = "template_kb"

# P2-7：来源类型标注（法规 / 行业规范 / 公司政策 / 范本），用于前端与报告中区分证据性质
KB_SOURCE_LABELS = {
    KBType.LEGAL.value: "法规",
    KBType.INDUSTRY.value: "行业规范",
    KBType.COMPANY.value: "公司政策",
    KBType.TEMPLATE.value: "范本",
}

def source_type_label(kb_type) -> str:
    return KB_SOURCE_LABELS.get(str(kb_type or "").lower(), "资料")

@dataclass
class KnowledgeRecord:
    knowledge_id: str
    kb_type: str
    document_id: str
    document_version: str
    title: str
    content: str
    citation: Optional[str] = None
    contract_types: List[str] = field(default_factory=list)
    clause_dimensions: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    source_url: Optional[str] = None
    effective_from: Optional[str] = None
    effective_to: Optional[str] = None
    status: str = "draft"
    authority_level: str = "reference"
    review_status: str = "draft"
    replacement_clause: Optional[str] = None
    knowledge_type: Optional[str] = None
    source: Optional[str] = None
    article_or_clause: Optional[str] = None
    version: Optional[str] = None
    effective_date: Optional[str] = None
    category: Optional[str] = None
    applicability_scope: str = "general"

@dataclass
class RetrievalRequest:
    query: str
    contract_type: Optional[str] = None
    clause_dimension: Optional[str] = None
    party_position: Optional[str] = None
    required_kb_types: Optional[List[str]] = None
    effective_at: Optional[str] = None
    top_k_per_kb: int = 3
    threshold: Optional[float] = None

@dataclass
class Evidence:
    evidence_id: str
    kb_type: str
    document_id: str
    document_version: str
    title: str
    citation: Optional[str]
    excerpt: str
    score: float
    authority_level: str
    validity: str = "valid"
    source_url: Optional[str] = None
    replacement_clause: Optional[str] = None
    source: Optional[str] = None
    article_or_clause: Optional[str] = None
    category: Optional[str] = None
    applicability_scope: str = "general"
    tags: List[str] = field(default_factory=list)
    clause_dimensions: List[str] = field(default_factory=list)
    contract_types: List[str] = field(default_factory=list)
    def to_dict(self) -> Dict[str, Any]: return asdict(self)

def normalize_record(raw: Dict[str, Any], kb_type: str, defaults: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    defaults = defaults or {}; record = dict(raw)
    record["knowledge_id"] = record.get("knowledge_id", record.get("id"))
    record["document_id"] = record.get("document_id", record["knowledge_id"])
    record["document_version"] = record.get("document_version", "v1")
    record["kb_type"] = kb_type; record["citation"] = record.get("citation", record.get("title"))
    for key in ("contract_types", "clause_dimensions", "tags"): record[key] = record.get(key, defaults.get(key, []))
    for key in ("source_url", "effective_from", "effective_to"): record[key] = record.get(key, defaults.get(key))
    record["status"] = record.get("status", defaults.get("status", "published"))
    record["authority_level"] = record.get("authority_level", defaults.get("authority_level", "reference"))
    record["review_status"] = record.get("review_status", defaults.get("review_status", "published"))
    record["replacement_clause"] = record.get("replacement_clause")
    record["knowledge_type"] = record.get("knowledge_type")
    record["source"] = record.get("source", defaults.get("source"))
    record["article_or_clause"] = record.get("article_or_clause")
    record["version"] = record.get("version", record.get("document_version", defaults.get("version")))
    record["effective_date"] = record.get("effective_date", defaults.get("effective_date"))
    record["category"] = record.get("category", defaults.get("category"))
    record["applicability_scope"] = record.get("applicability_scope", defaults.get("applicability_scope", "general"))
    if not record.get("source") or not record.get("article_or_clause"):
        ext_src, ext_art = _extract_source_and_article(record.get("title", ""), record.get("citation", ""))
        if not record.get("source"):
            record["source"] = ext_src
        if not record.get("article_or_clause"):
            record["article_or_clause"] = ext_art
    return record

def as_evidence(result: Dict[str, Any]) -> Evidence:
    chunk = result.get("chunk", result)
    return Evidence(
        f"{chunk.get('kb_type', KBType.LEGAL.value)}:{chunk.get('knowledge_id', chunk.get('id', 'unknown'))}",
        chunk.get("kb_type", KBType.LEGAL.value),
        chunk.get("document_id", chunk.get("id", "unknown")),
        chunk.get("document_version", "v1"),
        chunk.get("title", "未命名依据"),
        chunk.get("citation"),
        chunk.get("content", ""),
        float(result.get("score", 0.0)),
        chunk.get("authority_level", "reference"),
        result.get("validity", "valid"),
        chunk.get("source_url"),
        chunk.get("replacement_clause"),
        chunk.get("source"),
        chunk.get("article_or_clause"),
        chunk.get("category"),
        chunk.get("applicability_scope", "general"),
        list(chunk.get("tags") or []),
        list(chunk.get("clause_dimensions") or []),
        list(chunk.get("contract_types") or []),
    )
