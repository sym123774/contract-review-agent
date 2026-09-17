import json

from agent import pipeline
from agent.pipeline import ContractReviewAgent
from core.template_markers import mark_template_clauses
from core.clause_checks import check_structured_clauses
from core.rules import check_template_selections


def test_parenthetical_form_guidance_is_marked_as_instruction():
    clauses = [
        {"clause_id":"c001", "title":"工作条件",
         "content":"（履行合同的工作条件是指场所、工具等，并应注明由何方提供和承担费用。）"},
        {"clause_id":"c002", "title":"实际约定", "content":"甲方应在十日内付款。"},
    ]
    mark_template_clauses(clauses)
    assert clauses[0]["template_status"] == "instruction"
    assert clauses[1]["template_status"] == "active"


def test_instruction_clauses_do_not_enter_rules_or_model_coverage(monkeypatch):
    parsed = {"title":"测试合同", "full_text":"模板说明\n甲方应在十日内付款。", "warnings":[],
              "clauses":[
                  {"clause_id":"c001", "title":"违约金说明",
                   "content":"（双方应明确约定违约责任，可以约定每日5‰违约金。）",
                   "text":"[c001] 违约金说明\n（双方应明确约定违约责任，可以约定每日5‰违约金。）"},
                  {"clause_id":"c002", "title":"付款",
                   "content":"甲方应在十日内付款。", "text":"[c002] 付款\n甲方应在十日内付款。"},
              ]}
    seen = {}

    def fake_rules(rule_parsed, _party):
        seen["ids"] = [c["clause_id"] for c in rule_parsed["clauses"]]
        seen["text"] = rule_parsed["full_text"]
        return []

    class EmptyLLM:
        model = "empty"
        def __init__(self): self.usage_log = []
        def chat(self, _messages):
            self.usage_log.append({"kind":"chat", "prompt_tokens":1, "completion_tokens":1})
            return json.dumps([])
        def usage_summary(self, start=0, end=None):
            return {"model":self.model, "chat_calls":len(self.usage_log[start:end])}

    monkeypatch.setattr(pipeline, "parse_docx", lambda _: parsed)
    monkeypatch.setattr(pipeline, "infer_contract_type", lambda _: "general")
    monkeypatch.setattr(pipeline, "run_rule_engine", fake_rules)
    result = ContractReviewAgent(llm_client=EmptyLLM(), enable_kb=False).review("unused.docx")
    assert seen["ids"] == ["c002"]
    assert "5‰" not in seen["text"]
    assert result["completeness"] == "complete"
    assert result["coverage"]["excluded_template_clauses"] == ["c001"]
    assert result["coverage"]["llm_applicable_clauses"] == ["c002"]


def test_heading_continuity_can_use_excluded_instruction_headings():
    all_clauses = [
        {"clause_id":"c001", "title":"第一条 说明", "content":"（填写说明。）"},
        {"clause_id":"c002", "title":"第二条 约定", "content":"实际内容。"},
    ]
    parsed = {"clauses":[all_clauses[1]], "heading_clauses":all_clauses}
    risks = check_structured_clauses(parsed)
    assert not any("跳号" in risk.title for risk in risks)


def test_visible_unselected_payment_and_ip_choices_become_deterministic_risks():
    parsed = {"clauses":[
        {"clause_id":"c006", "title":"付款方式",
         "content":"以下两种方式任选一种：方式一验收后付款；方式二自拟。"},
        {"clause_id":"c007", "title":"成果归属",
         "content":"申请专利的权利按以下第____项执行："},
    ]}
    risks = check_template_selections(parsed)
    assert [(risk.title, risk.level.value) for risk in risks] == [
        ("付款方式选项未选择", "中风险"),
        ("知识产权归属选项未选择", "高风险"),
    ]
