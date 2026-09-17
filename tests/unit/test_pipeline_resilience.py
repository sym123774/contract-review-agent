import json

import pytest

from agent import pipeline
from agent.pipeline import ContractReviewAgent, ModelOutputError


def make_item(index, quote="甲方应于验收后付款。"):
    return {
        "clause_id": "c001",
        "level": "中风险",
        "title": f"风险{index}",
        "description": f"第{index}项说明",
        "original_text": quote,
        "suggestion": "补充明确约定",
        "confidence": "中",
        "risk_type": "commercial",
        "evidence": [],
    }


def agent_for_parsing():
    agent = ContractReviewAgent(enable_llm=False, enable_kb=False)
    agent.parsed = {"clauses": [{"clause_id": "c001", "title": "付款",
                                  "content": "甲方应于验收后付款。"}]}
    agent._active_ids = {"c001"}
    agent._offered_evidence = {}
    agent._offered_order = []
    return agent


def test_one_invalid_item_does_not_discard_five_valid_items():
    agent = agent_for_parsing()
    items = [make_item(i) for i in range(1, 6)] + [make_item(6, "并不存在的摘录")]
    risks = agent._parse_llm_response(json.dumps(items, ensure_ascii=False), "商业")
    assert len(risks) == 5
    assert agent._last_parse_diagnostics == {
        "received": 6,
        "accepted": 5,
        "rejected": 1,
        "rejections": [{"item": 6, "title": "风险6", "reason": "模型引用的合同原文无法定位"}],
    }


def test_all_invalid_items_still_fail_the_batch():
    agent = agent_for_parsing()
    with pytest.raises(ModelOutputError, match="均未通过逐条校验"):
        agent._parse_llm_response(json.dumps([make_item(1, "错误摘录")], ensure_ascii=False), "商业")
    assert agent._last_parse_diagnostics["rejected"] == 1


class RetryLLM:
    model = "retry-test"
    usage_log = []

    def __init__(self):
        self.usage_log = []
        bad = make_item(1, "无法定位的改写引文")
        fixed = make_item(1, "甲方应于验收后付款。")
        self.responses = [json.dumps([bad], ensure_ascii=False),
                          json.dumps([fixed], ensure_ascii=False), "[]"]

    def chat(self, _messages):
        response = self.responses.pop(0)
        self.usage_log.append({"kind":"chat", "model":self.model,
                               "prompt_tokens":1, "completion_tokens":1})
        return response

    def usage_summary(self, start=0, end=None):
        return {"model":self.model, "chat_calls":len(self.usage_log[start:end])}


def test_retriable_quote_error_gets_one_strict_correction_attempt(monkeypatch):
    parsed = {"title":"测试合同", "full_text":"甲方应于验收后付款。", "warnings":[],
              "clauses":[{"clause_id":"c001", "title":"付款",
                           "content":"甲方应于验收后付款。",
                           "text":"[c001] 付款\n甲方应于验收后付款。"}]}
    monkeypatch.setattr(pipeline, "parse_docx", lambda _: parsed)
    monkeypatch.setattr(pipeline, "infer_contract_type", lambda _: "general")
    monkeypatch.setattr(pipeline, "run_rule_engine", lambda *_: [])
    result = ContractReviewAgent(llm_client=RetryLLM(), enable_kb=False).review("unused.docx")
    assert result["completeness"] == "complete"
    assert result["batches"][0]["legal_items"]["recovered"] == 1
    assert result["batches"][0]["legal_items"]["rejected"] == 0
    assert len(result["commercial_risks"]) == 1


def test_isolated_rejection_is_a_completed_review_with_warning(monkeypatch):
    parsed = {"title":"测试合同", "full_text":"甲方应于验收后付款。", "warnings":[],
              "clauses":[{"clause_id":"c001", "title":"付款",
                           "content":"甲方应于验收后付款。",
                           "text":"[c001] 付款\n甲方应于验收后付款。"}]}
    good = make_item(1)
    bad = make_item(2, "无法定位的改写引文")

    class MixedLLM(RetryLLM):
        def __init__(self):
            self.usage_log = []
            self.responses = [json.dumps([good, bad], ensure_ascii=False), "[]"]

    monkeypatch.setattr(pipeline, "parse_docx", lambda _: parsed)
    monkeypatch.setattr(pipeline, "infer_contract_type", lambda _: "general")
    monkeypatch.setattr(pipeline, "run_rule_engine", lambda *_: [])
    monkeypatch.setattr(pipeline, "LLM_ITEM_RETRY_LIMIT", 0)
    result = ContractReviewAgent(llm_client=MixedLLM(), enable_kb=False).review("unused.docx")
    assert result["completeness"] == "complete"
    assert not result["review_errors"]
    assert result["review_warnings"]
    assert result["batches"][0]["legal"] == "completed_with_rejections"
