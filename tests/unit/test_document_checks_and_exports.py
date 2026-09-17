import asyncio

import pytest
from fastapi import HTTPException

import api_server
from core.document_checks import build_document_checklist
from core.template_markers import mark_template_clauses


def test_whole_document_checklist_distinguishes_not_found_from_legal_conclusion():
    clauses = [
        {"clause_id":"c001", "title":"第一条 费用", "content":"甲方向乙方支付研发报酬。"},
        {"clause_id":"c002", "title":"第二条 知识产权", "content":"开发成果申请权归甲方。"},
    ]
    parsed = {"title":"技术开发合同", "full_text":"\n".join(c["content"] for c in clauses),
              "clauses":clauses}
    checklist = build_document_checklist(parsed, "technology_development")
    by_key = {item["key"]:item for item in checklist["items"]}
    assert by_key["price"]["status"] == "present"
    assert by_key["ip"]["status"] == "present"
    assert by_key["acceptance"]["status"] == "not_found"
    assert "不直接等同于违法" in checklist["notice"]


def test_unselected_template_options_do_not_satisfy_whole_document_checklist():
    clauses = [
        {"clause_id":"c001", "title":"第七条 选择", "content":"按以下第____项执行："},
        {"clause_id":"c002", "title":"1、成果归甲方", "content":"专利申请权归甲方。"},
        {"clause_id":"c003", "title":"2、成果归乙方", "content":"专利申请权归乙方。"},
    ]
    mark_template_clauses(clauses)
    parsed = {"title":"技术开发合同", "full_text":"\n".join(c["content"] for c in clauses),
              "clauses":clauses}
    checklist = build_document_checklist(parsed, "technology_development")
    by_key = {item["key"]:item for item in checklist["items"]}
    assert clauses[1]["template_status"] == "unselected_option"
    assert clauses[2]["template_status"] == "unselected_option"
    assert by_key["ip"]["status"] == "not_found"


def test_partial_and_degraded_full_review_cannot_export_polished_report(monkeypatch):
    partial = {"completeness":"partial", "requested_mode":"full"}
    degraded = {"completeness":"rules_only", "requested_mode":"full"}
    intentional = {"completeness":"rules_only", "requested_mode":"rules_only"}
    assert not api_server.export_control(partial)["polished_report_allowed"]
    assert not api_server.export_control(degraded)["polished_report_allowed"]
    assert api_server.export_control(intentional)["polished_report_allowed"]

    monkeypatch.setattr(api_server, "_load_task_result", lambda _: partial)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(api_server.export_report("00000000-0000-0000-0000-000000000000", "pdf"))
    assert exc.value.status_code == 409
    raw = asyncio.run(api_server.export_report("00000000-0000-0000-0000-000000000000", "json"))
    assert "INCOMPLETE_raw" in raw.headers["content-disposition"]


def test_status_search_and_domain_catalog_keep_inactive_domains_disabled(monkeypatch):
    class FakeLLM:
        model = "test-model"
        def is_available(self):
            return True
        def list_models(self):
            return ["qwen2.5-7b:latest", "qwen3.6:27b", "qwen3:32b",
                    "qwen2.5-72b-iq4xs:latest"]

    class FakeKB:
        metadata_by_domain = {
            "legal_kb": [{"status":"published", "review_status":"published", "kb_type":"legal_kb",
                          "knowledge_id":"civil_test", "title":"测试法条", "content":"付款规则"}],
            "industry_kb": [{"status":"published", "review_status":"published", "kb_type":"industry_kb",
                             "knowledge_id":"industry_test", "title":"测试行业条款", "content":"行业规则"}],
        }
        indexes = {"legal_kb": object(), "industry_kb": object()}
        warnings = []
        def _matches(self, record):
            return record["status"] == "published" and record["review_status"] == "published"
        def is_ready(self):
            return True
        def search(self, query, top_k, kb_type, use_vector):
            return [{"chunk":{"kb_type":kb_type, "knowledge_id":"x", "title":"t", "content":"c"},
                     "score":0.8, "validity":"valid", "retrieval_mode":"hybrid"}]

    kb = FakeKB()
    monkeypatch.setattr(api_server, "get_llm_client", lambda: FakeLLM())
    monkeypatch.setattr(api_server, "get_kb", lambda: kb)
    monkeypatch.setattr(api_server, "KB_REVIEW_DOMAINS", ["legal_kb"])

    status = asyncio.run(api_server.system_status())
    assert status["kb_domains"] == {"legal_kb": 1}
    assert status["kb_review_domains"] == ["legal_kb"]
    assert status["kb_source_total"] == 2
    assert [item["value"] for item in status["review_models"]] == [
        "qwen2.5-7b:latest", "qwen3.6:27b", "qwen3:32b", "qwen2.5-72b-iq4xs:latest"]
    domains = asyncio.run(api_server.kb_domains())
    assert [(item["key"], item["count"], item["enabled"]) for item in domains] == [
        ("legal_kb", 1, True),
        ("industry_kb", 0, False),
        ("company_kb", 0, False),
        ("template_kb", 0, False),
    ]
    results = asyncio.run(api_server.kb_search("付款", domain=None, top_k=10, mode="auto"))
    assert {item["kb"] for item in results["results"]} == {"legal_kb"}
    browse = asyncio.run(api_server.kb_search("", domain=None, top_k=1000, mode="auto"))
    assert browse["mode"] == "browse"
    assert browse["total"] == 1
    assert browse["results"][0]["kb"] == "legal_kb"
    with pytest.raises(HTTPException, match="当前未启用"):
        asyncio.run(api_server.kb_search("医疗器械", domain="industry_kb", top_k=10, mode="auto"))
