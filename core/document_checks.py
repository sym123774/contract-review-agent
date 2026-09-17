"""Whole-document topic coverage checks kept separate from risk conclusions."""


TOPICS = {
    "parties": ("合同主体", ("甲方", "乙方")),
    "price": ("价款或报酬", ("价款", "报酬", "费用", "合同金额", "合同总价", "付款")),
    "term": ("期限与时间", ("期限", "日期", "完成时间", "交付时间", "有效期")),
    "delivery": ("交付或履行", ("交付", "交货", "履行", "完成")),
    "acceptance": ("验收或检验", ("验收", "检验", "测试", "确认合格")),
    "breach": ("违约责任", ("违约", "赔偿", "逾期利息", "罚息", "滞纳金", "逾期责任")),
    "termination": ("解除与终止", ("解除", "终止")),
    "dispute": ("争议解决", ("争议", "仲裁", "诉讼", "人民法院", "管辖")),
    "confidentiality": ("保密义务", ("保密", "秘密信息", "商业秘密")),
    "ip": ("知识产权与成果归属", ("知识产权", "专利", "著作权", "成果归属", "申请权")),
    "failure_risk": ("研发失败风险", ("开发失败", "研发失败", "技术风险", "技术困难")),
    "return_destroy": ("保密资料返还或销毁", ("返还", "销毁", "删除", "归还")),
    "exceptions": ("保密例外", ("不属于保密", "保密信息不包括", "例外", "公开信息", "合法获得")),
}

SCENARIOS = {
    "nda": ("parties", "term", "confidentiality", "return_destroy", "exceptions", "breach", "dispute"),
    "purchase": ("parties", "price", "term", "delivery", "acceptance", "breach", "termination", "dispute"),
    "technology_development": ("parties", "price", "term", "delivery", "acceptance", "ip",
                               "failure_risk", "confidentiality", "breach", "termination", "dispute"),
    "technology_transfer_license": ("parties", "price", "term", "delivery", "acceptance", "ip",
                                     "confidentiality", "breach", "termination", "dispute"),
    "technology_consulting_service": ("parties", "price", "term", "delivery", "acceptance",
                                       "confidentiality", "breach", "termination", "dispute"),
    "transport": ("parties", "price", "term", "delivery", "acceptance", "breach", "termination", "dispute"),
    "general": ("parties", "price", "term", "delivery", "breach", "termination", "dispute"),
}


def infer_check_scenario(parsed, contract_type):
    text = ((parsed or {}).get("title", "") + "\n" + (parsed or {}).get("full_text", ""))
    if any(value in text for value in ("保密协议", "保密合同", "NDA", "秘密信息")):
        return "nda"
    if contract_type and contract_type != "general":
        return contract_type
    if any(value in text for value in ("采购合同", "买卖合同", "购销合同", "采购人", "供应商", "货物")):
        return "purchase"
    return "general"


def build_document_checklist(parsed, contract_type):
    clauses = [clause for clause in ((parsed or {}).get("clauses") or [])
               if clause.get("template_status", "active") not in ("instruction", "unselected_option")]
    scenario = infer_check_scenario(parsed, contract_type)
    items = []
    for key in SCENARIOS.get(scenario, SCENARIOS["general"]):
        label, words = TOPICS[key]
        locations = []
        for clause in clauses:
            text = (clause.get("title", "") + "\n" + clause.get("content", ""))
            if key == "parties":
                matched = all(word in text for word in words)
            else:
                matched = any(word in text for word in words)
            if matched:
                locations.append(clause.get("clause_id"))
        items.append({
            "key": key,
            "label": label,
            "status": "present" if locations else "not_found",
            "clause_ids": [value for value in locations if value][:12],
        })
    return {
        "scenario": scenario,
        "items": items,
        "not_found": [item["key"] for item in items if item["status"] == "not_found"],
        "notice": "仅表示关键词和条款位置是否检出；未检出项需要人工确认，不直接等同于违法、无效或合同缺失。",
    }
