from pathlib import Path

import config
from agent import pipeline
from agent.prompts import build_contract_overview, build_system_prompt
from knowledge_base.retriever import KnowledgeBase


class RecordingLLM:
    model = "test-model"
    embedding_model = "bge-m3"

    def __init__(self):
        self.messages = []
        self.usage_log = []

    def chat(self, messages):
        self.messages.append(messages)
        return "```json\n[]\n```"

    def usage_summary(self, start=0, end=None):
        return {"model": self.model, "chat_calls": len(self.messages)}

    def embed(self, _):
        raise AssertionError("unit test must not call an embedding service")


def sample_parsed():
    clauses = [
        {"clause_id": "c001", "title": "一、付款", "content": "甲方应支付10000元。", "text": "[c001] 一、付款\n甲方应支付10000元。"},
        {"clause_id": "c002", "title": "二、验收", "content": "乙方交付后五日内验收。", "text": "[c002] 二、验收\n乙方交付后五日内验收。"},
    ]
    return {"title": "测试合同", "full_text": "\n".join(c["text"] for c in clauses), "clauses": clauses, "warnings": []}


def test_system_prompt_uses_manual_stance_without_fixed_bias():
    commercial = build_system_prompt("商业", {"our_side": "乙", "our_roles": [], "we_pay": False})
    legal = build_system_prompt("法律", {})
    assert "我方是合同乙方" in commercial
    assert "我方为收款方" in commercial
    assert "站在委托方立场" not in commercial
    assert "医疗器械行业合规" not in legal
    assert "中立审查" in legal


def test_contract_overview_records_topics_from_other_clauses():
    overview = build_contract_overview(sample_parsed())
    assert "付款：c001" in overview
    assert "验收：c002" in overview
    assert "10000元" in overview


def test_pipeline_marks_input_as_batch_and_passes_global_overview(monkeypatch):
    parsed = sample_parsed()
    monkeypatch.setattr(pipeline, "parse_docx", lambda _: parsed)
    monkeypatch.setattr(pipeline, "infer_contract_type", lambda _: "general")
    monkeypatch.setattr(pipeline, "run_rule_engine", lambda *_: [])
    llm = RecordingLLM()
    result = pipeline.ContractReviewAgent(llm_client=llm, enable_kb=False).review(
        "unused.docx", our_side="乙", we_pay=False
    )
    assert result["completeness"] == "complete"
    assert len(llm.messages) == 2
    for messages in llm.messages:
        assert "我方是合同乙方" in messages[0]["content"]
        assert "当前审核批次（只是全文的一部分）" in messages[1]["content"]
        assert "验收：c002" in messages[1]["content"]
        assert "合同条款全文" not in messages[1]["content"]
        assert "不得因为某项内容未出现在当前批次" in messages[0]["content"]


def test_realistic_contract_prompts_stay_within_configured_context():
    root = Path(__file__).resolve().parents[2]
    path = root / "docs/evidence/real_model_audit_20260915/inputs/usst.docx"
    llm = RecordingLLM()
    kb = KnowledgeBase(llm_client=llm)
    kb.load()
    result = pipeline.ContractReviewAgent(llm_client=llm, knowledge_base=kb).review(
        str(path), our_side="甲", we_pay=True
    )
    limit = config.LLM_NUM_CTX - config.LLM_MAX_TOKENS - 128
    estimates = [value for batch in result["batches"]
                 for value in batch["prompt_estimated_tokens"].values()]
    assert estimates
    assert max(estimates) <= limit
    assert all(batch["legal"] == "completed" and batch["commercial"] == "completed"
               for batch in result["batches"])


def test_rules_only_batching_does_not_depend_on_selected_model_context(monkeypatch):
    parsed = sample_parsed()
    parsed["clauses"][0]["content"] = "付款安排" * 120
    parsed["clauses"][0]["text"] = "[c001] 一、付款\n" + parsed["clauses"][0]["content"]
    parsed["full_text"] = "\n".join(c["text"] for c in parsed["clauses"])
    monkeypatch.setattr(pipeline, "parse_docx", lambda _: parsed)
    monkeypatch.setattr(pipeline, "infer_contract_type", lambda _: "general")
    monkeypatch.setattr(pipeline, "run_rule_engine", lambda *_: [])
    llm = RecordingLLM()
    llm.num_ctx = 4096
    result = pipeline.ContractReviewAgent(
        llm_client=llm, enable_llm=False, enable_kb=False
    ).review("unused.docx")
    assert len(result["batches"]) == 1
