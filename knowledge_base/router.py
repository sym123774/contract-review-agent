"""Contract-scenario and knowledge-domain routing."""
from typing import List, Optional
from config import KB_REVIEW_DOMAINS
from knowledge_base.models import KBType
GENERAL_DOMAINS = [KBType.LEGAL.value, KBType.COMPANY.value, KBType.TEMPLATE.value]
ALL_DOMAINS = [KBType.LEGAL.value, KBType.INDUSTRY.value, KBType.COMPANY.value, KBType.TEMPLATE.value]
def infer_contract_type(text: str) -> str:
    text = text or ""
    if any(k in text for k in ["保密协议", "保密合同", "NDA", "秘密信息"]):
        return "nda"
    medical = any(k in text for k in ["医疗器械", "医疗设备", "注册证", "备案人", "药品监督管理"])
    if medical and any(k in text for k in ["委托生产", "受托生产"]):
        return "medical_device_commissioned_manufacturing"
    if medical:
        return "medical_device_general"
    if any(k in text for k in ["运输合同", "承运人", "托运人", "运费", "货运", "客运", "多式联运"]):
        return "transport"
    if any(k in text for k in ["技术开发", "研究开发", "研发成果", "开发成果"]):
        return "technology_development"
    if any(k in text for k in ["技术转让", "技术许可", "专利许可", "许可使用"]):
        return "technology_transfer_license"
    if any(k in text for k in ["技术咨询", "技术服务", "技术顾问"]):
        return "technology_consulting_service"
    return "general"
def domains_for(contract_type: Optional[str], clause_dimension: Optional[str] = None,
                allowed: Optional[List[str]] = None) -> List[str]:
    """场景路由出的知识域，再按 allowed 白名单收口。

    allowed=None 时使用 config.KB_REVIEW_DOMAINS（默认仅法规域）；
    显式传入 allowed=ALL_DOMAINS 可恢复到四域全检（测试与离线脚本用）。
    收口后若为空，退回法规域，保证审核永远有法律依据可引。
    """
    if contract_type in {"medical_device_commissioned_manufacturing", "medical_device_general",
                         "technology_development", "technology_transfer_license",
                         "technology_consulting_service", "transport"}:
        selected = list(ALL_DOMAINS)
    elif clause_dimension in {"合法性合规性", "行业合规", "数据合规"}:
        selected = list(ALL_DOMAINS)
    else:
        selected = list(GENERAL_DOMAINS)
    permitted = KB_REVIEW_DOMAINS if allowed is None else allowed
    restricted = [d for d in selected if d in permitted]
    return restricted or [KBType.LEGAL.value]
