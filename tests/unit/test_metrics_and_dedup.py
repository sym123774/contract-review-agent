import json

from agent.pipeline import ContractReviewAgent
from core.llm_client import LLMClient
from core.rules import RiskItem, RiskLevel


def make_risk(risk_id, source, title, description, quote, clause="c001", basis=None):
    return RiskItem(risk_id, RiskLevel.HIGH, "合同条款", title, description,
                    clause_ref=clause, original_text=quote, source=source,
                    legal_basis=basis, evidence=[])


def test_chinese_confidence_is_preserved_as_numeric_signal():
    agent = ContractReviewAgent(enable_llm=False, enable_kb=False)
    quote = "甲方逾期每日支付2%违约金。"
    agent.parsed = {"clauses": [{"clause_id": "c001", "title": "违约责任", "content": quote}]}
    agent._active_ids = {"c001"}
    items = []
    for index, confidence in enumerate(("高", "中", "低"), 1):
        items.append({"clause_id":"c001", "level":"中风险", "title":f"风险{index}",
                      "description":"说明", "original_text":quote, "suggestion":"修改",
                      "confidence":confidence, "evidence":[]})
    risks = agent._parse_llm_response(json.dumps(items, ensure_ascii=False), "法律")
    assert [r.confidence for r in risks] == [.9, .7, .4]


def test_usage_summary_can_be_limited_to_one_review_session():
    client = LLMClient(model="test")
    client.usage_log = [
        {"kind":"chat", "model":"test", "prompt_tokens":100, "completion_tokens":20,
         "total_tokens":120, "seconds":2, "tokens_per_second":10},
        {"kind":"chat", "model":"test", "prompt_tokens":30, "completion_tokens":10,
         "total_tokens":40, "seconds":1, "tokens_per_second":10},
    ]
    assert client.usage_summary()["prompt_tokens"] == 130
    current = client.usage_summary(start=1)
    assert current["chat_calls"] == 1
    assert current["prompt_tokens"] == 30
    assert current["completion_tokens"] == 10


def test_llm_client_keeps_per_review_context_limit():
    client = LLMClient(model="qwen2.5-72b-iq4xs:latest", num_ctx=4096)
    assert client.model == "qwen2.5-72b-iq4xs:latest"
    assert client.num_ctx == 4096


def test_rule_legal_and_commercial_duplicate_is_counted_once():
    quote = "甲方逾期每日支付2%违约金。"
    agent = ContractReviewAgent(enable_llm=False, enable_kb=False)
    agent.parsed = {"clauses": [{"clause_id":"c001", "title":"违约责任", "content":quote}]}
    agent.rule_risks = [make_risk("rule", "rule", "周期费用或违约金偏高（单期2%）", "每日2%", quote)]
    agent.legal_risks = [make_risk("legal", "llm_法律", "违约金比例过高", "每日2%可能被调减", quote,
                                   basis="《民法典》第五百八十五条")]
    agent.commercial_risks = [make_risk("commercial", "llm_商业", "每日违约金过高", "每日2%成本过高", quote)]
    agent._dedupe_cross_route()
    combined = agent.rule_risks + agent.legal_risks + agent.commercial_risks
    assert len(combined) == 1
    assert combined[0].risk_type_tag == "penalty_rate"
    assert combined[0].legal_basis


def test_late_payment_interest_and_penalty_are_one_risk():
    quote = "甲方逾期付款每日按合同金额5‰支付逾期付款利息。"
    agent = ContractReviewAgent(enable_llm=False, enable_kb=False)
    agent.parsed = {"clauses": [{"clause_id": "c001", "title": "违约责任", "content": quote}]}
    agent.rule_risks = [make_risk("rule", "rule", "周期费用或违约金偏高（单期0.5%）", "每日0.5%", quote)]
    agent.legal_risks = [make_risk("legal", "llm_法律", "逾期付款利息约定过高", "每日5‰比例过高", quote)]
    agent.commercial_risks = []
    agent._dedupe_cross_route()
    assert len(agent.rule_risks + agent.legal_risks) == 1


def test_rule_wording_survives_model_merge_and_keeps_only_distinct_related_evidence():
    quote = "任何情况下，乙方对质量损失不承担赔偿责任。"
    agent = ContractReviewAgent(enable_llm=False, enable_kb=False)
    agent.parsed = {"clauses": [{"clause_id":"c001", "title":"责任限制", "content":quote}]}
    rule = make_risk("rule", "rule", "绝对免除质量责任存在风险", "规则确定性说明", quote)
    model = make_risk("model", "llm_法律", "免责条款当然无效", "模型扩大解释", quote,
                      basis="《民法典》第五百零六条")
    model.evidence = ["legal_kb:civil_506"]
    model.related_evidence = ["legal_kb:civil_506", "legal_kb:pql_41"]
    agent.rule_risks = [rule]
    agent.legal_risks = [model]
    agent._dedupe_cross_route()
    assert len(agent.rule_risks) == 1
    merged = agent.rule_risks[0]
    assert merged.title == "绝对免除质量责任存在风险"
    assert merged.description == "规则确定性说明"
    assert merged.evidence == ["legal_kb:civil_506"]
    assert merged.related_evidence == ["legal_kb:pql_41"]


def test_derived_annual_penalty_percentage_does_not_split_same_source_fact():
    quote = "甲方逾期付款每日按合同金额5‰支付逾期付款利息。"
    agent = ContractReviewAgent(enable_llm=False, enable_kb=False)
    agent.parsed = {"clauses": [{"clause_id":"c001", "title":"违约责任", "content":quote}]}
    agent.rule_risks = [make_risk("rule", "rule", "周期费用或违约金偏高（单期0.5%）", "每日0.5%", quote)]
    agent.commercial_risks = [make_risk("model", "llm_商业", "逾期付款利息过高",
        "每日5‰，简单年化约182.5%", quote)]
    agent._dedupe_cross_route()
    assert len(agent.rule_risks + agent.commercial_risks) == 1


def test_different_penalty_rates_in_same_clause_are_not_merged():
    agent = ContractReviewAgent(enable_llm=False, enable_kb=False)
    agent.parsed = {"clauses": []}
    agent.legal_risks = [
        make_risk("a", "llm_法律", "违约金比例过高", "每日2%", "逾期交付每日支付2%违约金。"),
        make_risk("b", "llm_法律", "违约金比例过高", "每日0.5%", "逾期付款每日支付0.5%违约金。"),
    ]
    agent._dedupe_cross_route()
    assert len(agent.legal_risks) == 2


def test_dingjin_correction_merges_routes_without_claiming_verification():
    quote = "合同签订当日，甲方向乙方支付订金80%。"
    agent = ContractReviewAgent(enable_llm=False, enable_kb=False)
    agent.legal_risks = [make_risk("a", "llm_法律", "订金性质约定不清", "退还条件不清", quote)]
    agent.commercial_risks = [make_risk("b", "llm_商业", "订金金额超过定金上限", "远超20%上限", quote,
                                        basis="民法典第五百八十六条")]
    agent._dedupe_cross_route()
    agent._correct_dingjin_violations()
    combined = agent.legal_risks + agent.commercial_risks
    assert len(combined) == 1
    assert combined[0].legal_basis is None
    assert combined[0].review_status == "needs_review"


def test_semantic_dedup_merges_same_confidentiality_facet_but_keeps_different_facts():
    quote = "保密信息范围包括技术资料，保密期限为五年。"
    agent = ContractReviewAgent(enable_llm=False, enable_kb=False)
    agent.parsed = {"clauses":[{"clause_id":"c001", "title":"保密", "content":quote}]}
    scope_a = make_risk("a", "llm_法律", "保密信息范围过宽", "保密定义范围不明确", quote)
    scope_b = make_risk("b", "llm_商业", "秘密信息定义宽泛", "保密范围可能扩大义务", quote)
    duration = make_risk("c", "llm_商业", "保密期限过长", "五年期限增加管理成本", quote)
    agent.legal_risks = [scope_a]
    agent.commercial_risks = [scope_b, duration]
    agent._dedupe_cross_route()
    combined = agent.legal_risks + agent.commercial_risks
    assert len(combined) == 2
    assert sorted(r.risk_type_tag for r in combined) == ["confidentiality", "confidentiality"]


def test_real_model_wording_duplicates_merge_by_fact_tag():
    cases = [
        ("付款方式未明确选择", "付款方式提供两种选项但未选择", "payment_selection"),
        ("付款条件未明确约定付款方式选择", "方式二自拟且为空", "payment_selection"),
        ("口头约定效力条款风险", "口头约定难以举证", "oral_agreement"),
        ("口头约定具备同等法律效力", "建议改为书面补充协议", "oral_agreement"),
        ("研发失败风险承担机制对我方不利", "研发失败仍按实际工作量支付报酬", "development_failure"),
        ("研发失败风险分配缺乏减损义务", "开发失败时双方互不追责", "development_failure"),
    ]
    agent = ContractReviewAgent(enable_llm=False, enable_kb=False)
    agent.parsed = {"clauses": []}
    agent.legal_risks = [make_risk("a", "llm_法律", cases[0][0], cases[0][1], "付款方式按以下两种方式任选一种。", "c006"),
                         make_risk("c", "llm_法律", cases[2][0], cases[2][1], "口头约定与合同具有同等效力。", "c015"),
                         make_risk("e", "llm_法律", cases[4][0], cases[4][1], "开发失败按实际工作量支付报酬。", "c012")]
    agent.commercial_risks = [make_risk("b", "llm_商业", cases[1][0], cases[1][1], "付款方式按以下两种方式任选一种。", "c006"),
                              make_risk("d", "llm_商业", cases[3][0], cases[3][1], "口头约定与合同具有同等效力。", "c015"),
                              make_risk("f", "llm_商业", cases[5][0], cases[5][1], "开发失败按实际工作量支付报酬。", "c012")]
    agent._dedupe_cross_route()
    combined = agent.legal_risks + agent.commercial_risks
    assert len(combined) == 3
    assert sorted(r.risk_type_tag for r in combined) == ["development_failure", "oral_agreement", "payment_selection"]


def test_ip_search_duty_duplicate_is_not_split_by_responsibility_wording():
    quote = "甲方投入生产前应进行知识产权检索，确保成果未落入他人知识产权保护范围。"
    agent = ContractReviewAgent(enable_llm=False, enable_kb=False)
    agent.parsed = {"clauses": [{"clause_id": "c012", "title": "知识产权", "content": quote}]}
    agent.legal_risks = [make_risk("a", "llm_法律", "甲方承担额外的知识产权检索义务",
        "若检索疏漏导致侵权，甲方可能面临第三方索赔。", quote, "c012")]
    agent.commercial_risks = [make_risk("b", "llm_商业", "知识产权检索增加履约成本",
        "若检索疏漏导致侵权，责任归属不明。", quote, "c012")]
    agent._dedupe_cross_route()
    combined = agent.legal_risks + agent.commercial_risks
    assert len(combined) == 1
    assert combined[0].risk_type_tag == "ip_ownership"


def test_payment_selection_beats_acceptance_wording_and_document_missing_clause_merges():
    quote = "付款方式任选一种：方式一验收合格后付款；方式二自拟。"
    agent = ContractReviewAgent(enable_llm=False, enable_kb=False)
    agent.parsed = {"clauses":[{"clause_id":"c006", "title":"付款", "content":quote}]}
    agent.legal_risks = [make_risk("a", "llm_法律", "付款方式未明确选择",
        "两种方式包含验收后付款但未选择", quote, "c006")]
    agent.commercial_risks = [make_risk("b", "llm_商业", "付款方式选择未定",
        "方式一和方式二未勾选", "付款方式任选一种。", "c006")]
    agent.rule_risks = [make_risk("r", "rule", "缺少必备条款：违约责任",
        "合同中未发现违约责任相关约定", "", None)]
    agent.commercial_risks.append(make_risk("c", "llm_商业", "合同中未约定违约责任条款",
        "全文缺少违约责任", "双方互不追究责任。", "c012"))
    agent._dedupe_cross_route()
    combined = agent.rule_risks + agent.legal_risks + agent.commercial_risks
    assert sum(r.risk_type_tag == "payment_selection" for r in combined) == 1
    assert sum(r.risk_type_tag == "missing_clause" for r in combined) == 1


def test_document_level_contract_scope_duplicates_merge():
    agent = ContractReviewAgent(enable_llm=False, enable_kb=False)
    agent.parsed = {"clauses":[]}
    agent.rule_risks = [make_risk("r", "rule", "缺少合同标的约定",
        "合同中未明确约定委托内容或服务范围", "", None)]
    agent.commercial_risks = [make_risk("m", "llm_商业", "合同标的未明确约定",
        "技术开发项目内容不明", "技术开发项目：____", "c001")]
    agent._dedupe_cross_route()
    assert len(agent.rule_risks + agent.commercial_risks) == 1
    assert agent.rule_risks[0].title == "缺少合同标的约定"


def test_blank_copy_count_wording_merges_with_document_blank_warning():
    agent = ContractReviewAgent(enable_llm=False, enable_kb=False)
    agent.parsed = {"clauses":[]}
    agent.rule_risks = [make_risk("r", "rule", "存在未填写的空白字段", "合同存在空白字段", "", None)]
    agent.commercial_risks = [make_risk("m", "llm_商业", "合同份数及正副本数量未明确",
        "一式几份等字段为空白", "本合同一式____份。", "c018")]
    agent._dedupe_cross_route()
    assert len(agent.rule_risks + agent.commercial_risks) == 1
