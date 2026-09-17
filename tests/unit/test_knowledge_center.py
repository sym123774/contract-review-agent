import json
from pathlib import Path
import pytest
from knowledge_base.registry import get_records_by_domain
from knowledge_base.router import infer_contract_type, domains_for, ALL_DOMAINS
from knowledge_base.retriever import KnowledgeBase
from knowledge_base.models import KBType, as_evidence
from agent.pipeline import ContractReviewAgent, ModelOutputError


def test_four_domains_have_metadata():
    domains = get_records_by_domain()
    assert set(domains) == {"legal_kb", "industry_kb", "company_kb", "template_kb"}
    for name, records in domains.items():
        assert records
        assert all(r["kb_type"] == name and r["document_version"] for r in records)


def test_routing_for_mvp_contracts():
    text = "医疗器械注册人委托受托生产，双方签订委托生产协议"
    assert infer_contract_type(text) == "medical_device_commissioned_manufacturing"
    # 场景本身路由到四域；审核链路默认只放开法规域
    assert domains_for("medical_device_commissioned_manufacturing", allowed=ALL_DOMAINS) == [
        "legal_kb", "industry_kb", "company_kb", "template_kb"]
    assert domains_for("medical_device_commissioned_manufacturing") == ["legal_kb"]
    assert infer_contract_type("技术开发成果转化及专利申请权") == "technology_development"
    assert infer_contract_type("专利技术许可使用") == "technology_transfer_license"
    assert infer_contract_type("提供技术咨询和顾问服务") == "technology_consulting_service"
    assert infer_contract_type("普通服装委托生产合同") == "general"
    assert infer_contract_type("医疗器械技术开发及注册证申报") == "medical_device_general"
    assert infer_contract_type("甲方委托承运人运输货物并支付运费") == "transport"
    assert infer_contract_type("档案保密协议（NDA）") == "nda"


def test_draft_records_never_pass_publication_filter():
    # 具体哪条是草稿随知识库迭代变化，这里断言不变量：非 published 记录不进默认检索
    kb = KnowledgeBase()
    kb.load()
    for domain, records in get_records_by_domain().items():
        for record in records:
            if record.get("status") != "published" or record.get("review_status") != "published":
                assert not kb._matches(record), f"{domain}/{record['knowledge_id']} 草稿记录被默认检索放行"


def test_llm_risk_without_evidence_requires_review():
    agent = ContractReviewAgent(enable_llm=False, enable_kb=False)
    agent.parsed = {"clauses": [{"clause_id": "c1", "title": "违约责任",
                                 "content": "甲方逾期每日按0.5%支付违约金"}]}
    agent._active_ids = {"c1"}
    agent._offered_evidence = {}
    item = {"title": "无依据结论", "level": "高风险", "description": "未引用任何知识中心证据",
            "suggestion": "补充法条依据后复核", "original_text": "甲方逾期每日按0.5%支付违约金",
            "clause_id": "c1"}
    risks = agent._parse_llm_response(json.dumps([item], ensure_ascii=False), "法律")
    assert risks[0].review_status == "needs_review"
    assert risks[0].evidence == []
    assert risks[0].evidence_status == "unsubstantiated"
    # 缺字段/非JSON 一律抛错，不能静默产出"0 项成功结果"
    with pytest.raises(ModelOutputError):
        agent._parse_llm_response(json.dumps([{"title": "无原文", "level": "高风险"}]), "法律")


def test_unselected_options_and_manual_stance_are_hard_validated():
    agent = ContractReviewAgent(enable_llm=False, enable_kb=False)
    clauses = [
        {"clause_id": "c7", "title": "成果归属", "content": "专利申请权按以下第____项执行："},
        {"clause_id": "c8", "title": "1、权利归乙方", "content": "甲方可以免费实施。"},
        {"clause_id": "c9", "title": "2、权利归甲方", "content": "乙方可以免费实施。"},
        {"clause_id": "c10", "title": "第七条 验收", "content": "验收合格后付款。"},
    ]
    agent.parsed = {"clauses": clauses}
    agent._active_ids = {c["clause_id"] for c in clauses}
    agent._unselected_option_ids = agent._find_unselected_option_ids(clauses)
    agent.party_context = {"our_side": "甲", "we_pay": True}
    base = {"clause_id": "c8", "level": "中风险", "title": "专利权归乙方",
            "description": "专利权已经归乙方，对我方不利", "original_text": "1、权利归乙方",
            "suggestion": "改为甲方所有", "evidence": []}
    with pytest.raises(ModelOutputError, match="未选择"):
        agent._parse_llm_item(base, "商业", {c["clause_id"]: c for c in clauses})
    conditional = dict(base, title="知识产权选项未明确",
                       description="当前未选择；若选择第1项，可能对我方不利")
    with pytest.raises(ModelOutputError, match="选择器条款"):
        agent._parse_llm_item(conditional, "商业", {c["clause_id"]: c for c in clauses})
    selector = dict(conditional, clause_id="c7",
                    original_text="专利申请权按以下第____项执行：")
    assert agent._parse_llm_item(selector, "商业", {c["clause_id"]: c for c in clauses})

    liability = {"c1": {"clause_id": "c1", "title": "解除", "content":
                         "如乙方要求解除合同，所造成的损失由乙方负责。"}}
    wrong = dict(base, clause_id="c1", title="解除责任对我方不利",
                 description="乙方解除时我方无法追责，对我方不利",
                 original_text="如乙方要求解除合同，所造成的损失由乙方负责。")
    agent._active_ids.add("c1")
    with pytest.raises(ModelOutputError, match="反向"):
        agent._parse_llm_item(wrong, "商业", liability)
    admits_favorable = dict(wrong, title="乙方解除的责任承担",
        description="虽然这看似保护甲方，但损失范围可能产生争议")
    with pytest.raises(ModelOutputError, match="有利安排"):
        agent._parse_llm_item(admits_favorable, "商业", liability)

    both = {"c2": {"clause_id": "c2", "title": "违约", "content":
                    "乙方逾期交货支付违约金；甲方逾期付款支付违约金。"}}
    own_risk = dict(base, clause_id="c2", title="甲方逾期付款违约金过高",
                    description="甲方逾期付款将承担高额违约金，对我方不利",
                    original_text="乙方逾期交货支付违约金；甲方逾期付款支付违约金。")
    agent._active_ids.add("c2")
    assert agent._parse_llm_item(own_risk, "商业", both)

    payee_text = "甲方逾期付款，每逾期一日，按未付款金额的2%向乙方支付违约金。"
    payee = {"c3": {"clause_id": "c3", "title": "违约", "content": payee_text}}
    payee_risk = dict(base, clause_id="c3", title="甲方逾期付款违约金过高",
                      description="甲方需支付高额违约金，对我方不利", original_text=payee_text)
    agent._active_ids.add("c3")
    assert agent._parse_llm_item(payee_risk, "商业", payee)

    fake_deposit = dict(base, clause_id="c3", title="订金退款条件不清",
                        description="订金无法退还，对我方不利", original_text=payee_text)
    with pytest.raises(ModelOutputError, match="虚构"):
        agent._parse_llm_item(fake_deposit, "商业", payee)

    payment = {"c4":{"clause_id":"c4", "title":"付款",
        "content":"货物验收合格后十个工作日内甲方向乙方付款。"}}
    reversed_payment = dict(base, clause_id="c4", title="付款条件依赖乙方",
        description="付款延迟对我方资金安排不利", original_text=payment["c4"]["content"])
    agent._active_ids.add("c4")
    with pytest.raises(ModelOutputError, match="验收后付款"):
        agent._parse_llm_item(reversed_payment, "商业", payment)

    acceptance = {"c5":{"clause_id":"c5", "title":"验收",
        "content":"如乙方不能到场，甲方有权开箱检验，乙方应认可并负责解决。"}}
    reversed_acceptance = dict(base, clause_id="c5", title="验收责任全部由甲方承担",
        description="该约定增加甲方责任", original_text=acceptance["c5"]["content"])
    agent._active_ids.add("c5")
    with pytest.raises(ModelOutputError, match="甲方承担全部责任"):
        agent._parse_llm_item(reversed_acceptance, "商业", acceptance)


def test_penalty_evidence_and_civil_858_claim_are_guarded():
    agent = ContractReviewAgent(enable_llm=False, enable_kb=False)
    clause = {"clause_id": "c1", "title": "违约责任",
              "content": "甲方逾期付款，每日按合同金额5‰支付逾期付款利息。"}
    agent.parsed = {"clauses": [clause]}
    agent._active_ids = {"c1"}
    agent.party_context = {"our_side": "甲", "we_pay": True}
    agent._offered_order = ["legal_kb:civil_845"]
    agent._offered_evidence = {"legal_kb:civil_845": {"kb_type": "legal_kb", "title": "技术合同主要条款",
                                                        "citation": "民法典第八百四十五条"}}
    item = {"clause_id": "c1", "level": "高风险", "title": "逾期付款利息比例过高",
            "description": "每日5‰的逾期付款利息比例过高", "original_text": clause["content"],
            "suggestion": "降低比例", "risk_type": "legal", "evidence": [1]}
    parsed = agent._parse_llm_item(item, "法律", {"c1": clause})
    assert parsed.evidence == []
    assert any("不直接适用" in issue for issue in parsed.validation_issues)

    agent._offered_order = ["legal_kb:civil_858"]
    agent._offered_evidence = {"legal_kb:civil_858": {"kb_type": "legal_kb", "title": "研发失败风险",
                                                        "citation": "民法典第八百五十八条"}}
    bad = dict(item, title="约定与民法典第八百五十八条冲突",
               description="风险分担违反第八百五十八条", evidence=[1])
    with pytest.raises(ModelOutputError, match="允许当事人约定"):
        agent._parse_llm_item(bad, "法律", {"c1": clause})
    reversed_words = dict(item, title="约定与民法典第八百五十八条存在冲突",
                          description="风险分担方式需要调整", evidence=[1])
    with pytest.raises(ModelOutputError, match="允许当事人约定"):
        agent._parse_llm_item(reversed_words, "法律", {"c1": clause})
    inconsistent = dict(item, title="约定与民法典第八百五十八条不一致",
                        description="风险分担方式需要调整", evidence=[1])
    with pytest.raises(ModelOutputError, match="允许当事人约定"):
        agent._parse_llm_item(inconsistent, "法律", {"c1": clause})


def test_general_article_cannot_support_default_consent_claim_and_unsubstantiated_high_is_downgraded():
    agent = ContractReviewAgent(enable_llm=False, enable_kb=False)
    quote = "解除通知送达对方时合同解除。"
    clause = {"clause_id":"c1", "title":"解除", "content":quote}
    agent.parsed = {"clauses":[clause]}
    agent._active_ids = {"c1"}
    agent._offered_order = ["legal_kb:civil_565"]
    agent._offered_evidence = {"legal_kb:civil_565": {"kb_type":"legal_kb",
        "title":"合同解除程序", "citation":"民法典第五百六十五条",
        "excerpt":"当事人依法主张解除合同的，应当通知对方。", "tags":["合同解除"]}}
    categorical = {"clause_id":"c1", "level":"高风险", "title":"逾期未答复视为同意不符合民法典规定",
        "description":"默认同意机制违法", "original_text":quote, "suggestion":"删除",
        "risk_type":"legal", "evidence":[1]}
    with pytest.raises(ModelOutputError, match="缺少直接法规支撑"):
        agent._parse_llm_item(categorical, "法律", {"c1":clause})
    difference = dict(categorical, title="解除程序与民法典第五百六十五条存在差异",
                      description="未答复视为同意与该条程序存在差异")
    with pytest.raises(ModelOutputError, match="缺少直接法规支撑"):
        agent._parse_llm_item(difference, "法律", {"c1":clause})

    clue = dict(categorical, title="解除通知安排可能产生争议", description="建议复核送达方式", evidence=[])
    parsed = agent._parse_llm_item(clue, "法律", {"c1":clause})
    assert parsed.level.value == "中风险"
    assert any("已降为中风险" in issue for issue in parsed.validation_issues)


def test_statutory_jurisdiction_wording_is_not_reported_as_missing_specific_court():
    agent = ContractReviewAgent(enable_llm=False, enable_kb=False)
    quote = "双方协商不成的，向有诉讼管辖权的法院提起诉讼。"
    clause = {"clause_id":"c1", "title":"争议解决", "content":quote}
    agent._active_ids = {"c1"}
    item = {"clause_id":"c1", "level":"低风险", "title":"管辖法院约定不明确",
            "description":"未指定具体法院，可能引发管辖争议", "original_text":quote,
            "suggestion":"指定某一法院", "risk_type":"legal", "evidence":[]}
    with pytest.raises(ModelOutputError, match="有管辖权法院"):
        agent._parse_llm_item(item, "法律", {"c1":clause})


def test_reciprocal_clause_cannot_be_rewritten_as_one_sides_action_and_batch_cannot_infer_missing_acceptance():
    agent = ContractReviewAgent(enable_llm=False, enable_kb=False)
    agent.party_context = {"our_side":"甲", "we_pay":True}
    reciprocal = "合同一方要求变更或解除时，应通知对方，对方未答复视为同意。"
    clause = {"clause_id":"c1", "title":"变更解除", "content":reciprocal}
    agent._active_ids = {"c1"}
    base = {"clause_id":"c1", "level":"中风险", "original_text":reciprocal,
            "suggestion":"完善约定", "risk_type":"commercial", "evidence":[]}
    wrong_side = dict(base, title="变更机制对我方不利",
                      description="若我方提出解除，对方沉默会使我方被动接受变更")
    with pytest.raises(ModelOutputError, match="对双方适用"):
        agent._parse_llm_item(wrong_side, "商业", {"c1":clause})
    wrong_recipient = dict(base, title="默示同意机制对我方不利",
                           description="对方接到变更通知后沉默，使我方失去最终决定权")
    with pytest.raises(ModelOutputError, match="对双方适用"):
        agent._parse_llm_item(wrong_recipient, "商业", {"c1":clause})

    missing = dict(base, title="合同未约定验收权",
                   description="未发现甲方验收成果的流程")
    with pytest.raises(ModelOutputError, match="不含验收约定"):
        agent._parse_llm_item(missing, "商业", {"c1":clause})
    hidden_missing = dict(base, title="研发失败责任对我方不利",
                          description="合同全文未检出验收标准，实际工作量难以认定")
    with pytest.raises(ModelOutputError, match="整份合同缺少验收"):
        agent._parse_llm_item(hidden_missing, "商业", {"c1":clause})


def test_template_risk_cannot_smuggle_unsupported_contract_effect_claim():
    agent = ContractReviewAgent(enable_llm=False, enable_kb=False)
    quote = "付款方式按以下两种方式任选一种执行。"
    clause = {"clause_id":"c1", "title":"付款", "content":quote}
    agent._active_ids = {"c1"}
    item = {"clause_id":"c1", "level":"中风险", "title":"付款方式未选择",
            "description":"选项空白可能导致合同效力待定", "original_text":quote,
            "suggestion":"明确选择", "risk_type":"template_deviation", "evidence":[]}
    with pytest.raises(ModelOutputError, match="缺少直接法规支撑"):
        agent._parse_llm_item(item, "商业", {"c1":clause})


def test_specialised_legal_claim_needs_specialised_authority():
    agent = ContractReviewAgent(enable_llm=False, enable_kb=False)
    clause = {"clause_id":"c1", "title":"保密管理",
              "content":"乙方人员查阅保密资料应经甲方批准。"}
    agent.parsed = {"clauses":[clause]}
    agent._active_ids = {"c1"}
    agent._offered_order = ["legal_kb:civil_501"]
    agent._offered_evidence = {
        "legal_kb:civil_501": {"kb_type":"legal_kb", "title":"缔约保密义务",
            "citation":"民法典第五百零一条", "excerpt":"当事人不得泄露订立合同中知悉的商业秘密。",
            "tags":["保密义务", "商业秘密"]}
    }
    item = {"clause_id":"c1", "level":"高风险", "title":"缺少国家秘密等级和涉密资质",
            "description":"该约定违反国家秘密管理要求", "original_text":clause["content"],
            "suggestion":"核实涉密等级", "risk_type":"legal", "evidence":[1]}
    with pytest.raises(ModelOutputError, match="缺少直接法规支撑"):
        agent._parse_llm_item(item, "法律", {"c1":clause})


@pytest.mark.parametrize("title,description", [
    (
        "涉及国家秘密的资质与合规风险",
        "处理国家秘密业务通常要求乙方具备相应涉密资质；若不具备法定资质，可能面临行政处罚。",
    ),
    (
        "沉默视为同意变更合同的约定存在效力不确定性",
        "该约定的法律效力存疑，可能需要通过诉讼确认。",
    ),
    (
        "合同变更解除的默认同意机制风险",
        "该机制与民法典关于解除通知的规定存在解释空间。",
    ),
    (
        "合同主体信息缺失",
        "当事人名称为空将导致合同无法生效。",
    ),
])
def test_variant_categorical_legal_claims_need_direct_authority(title, description):
    agent = ContractReviewAgent(enable_llm=False, enable_kb=False)
    clause = {"clause_id":"c1", "title":"合同约定", "content":"逾期未答复则视为已同意。"}
    agent.parsed = {"clauses":[clause]}
    agent._active_ids = {"c1"}
    item = {"clause_id":"c1", "level":"中风险", "title":title,
            "description":description, "original_text":clause["content"],
            "suggestion":"人工核查", "risk_type":"legal", "evidence":[]}
    with pytest.raises(ModelOutputError, match="缺少直接法规支撑"):
        agent._parse_llm_item(item, "法律", {"c1":clause})


def test_product_quality_scope_article_cannot_prove_liability_or_invalidity():
    agent = ContractReviewAgent(enable_llm=False, enable_kb=False)
    quote = "任何情况下，乙方对设备质量问题造成的损失不承担任何赔偿责任。"
    clause = {"clause_id":"c1", "title":"免责", "content":quote}
    agent.parsed = {"clauses":[clause]}
    agent._active_ids = {"c1"}
    agent._offered_order = ["legal_kb:pql_2"]
    agent._offered_evidence = {"legal_kb:pql_2": {"kb_type":"legal_kb",
        "title":"产品质量法第二条 适用范围与产品定义", "citation":"《产品质量法》第二条",
        "excerpt":"用于销售的加工产品适用本法。", "tags":["产品质量", "适用范围"]}}
    item = {"clause_id":"c1", "level":"高风险", "title":"免责条款违反产品质量法",
            "description":"该免责条款无效", "original_text":quote, "suggestion":"删除免责",
            "risk_type":"commercial", "evidence":[1]}
    with pytest.raises(ModelOutputError, match="缺少直接法规支撑"):
        agent._parse_llm_item(item, "商业", {"c1":clause})


def test_commercial_item_uses_law_only_as_related_material():
    agent = ContractReviewAgent(enable_llm=False, enable_kb=False)
    clause = {"clause_id":"c1", "title":"研发风险",
              "content":"研发失败时甲方按乙方实际工作量支付报酬。"}
    agent.parsed = {"clauses":[clause]}
    agent._active_ids = {"c1"}
    agent._offered_order = ["legal_kb:civil_858"]
    agent._offered_evidence = {
        "legal_kb:civil_858": {"kb_type":"legal_kb", "title":"研发失败风险负担",
            "citation":"民法典第八百五十八条", "excerpt":"风险负担由当事人约定。",
            "tags":["研发失败风险"]}
    }
    item = {"clause_id":"c1", "level":"中风险", "title":"研发失败成本对甲方不利",
            "description":"甲方可能仍需支付较高成本", "original_text":clause["content"],
            "suggestion":"增加里程碑和止损机制", "risk_type":"commercial", "evidence":[1]}
    parsed = agent._parse_llm_item(item, "法律", {"c1":clause})
    assert parsed.legal_basis is None
    assert parsed.evidence == []
    assert parsed.related_evidence == ["legal_kb:civil_858"]
    assert parsed.evidence_status == "topic_related"


def test_fixed_total_false_positive_is_blocked_from_either_model_route():
    agent = ContractReviewAgent(enable_llm=False, enable_kb=False)
    agent.party_context = {"our_side":"甲", "we_pay":True}
    quote = "本合同总价不得调整。"
    item = {"clause_id":"c1", "level":"中风险", "title":"固定总价对甲方不利",
            "description":"甲方承担成本增加风险", "original_text":quote,
            "suggestion":"允许调价", "risk_type":"commercial", "evidence":[]}
    agent._active_ids = {"c1"}
    with pytest.raises(ModelOutputError, match="固定总价"):
        agent._parse_llm_item(item, "法律", {"c1":{"clause_id":"c1", "title":"价款", "content":quote}})


def test_legal_scope_filters_specialized_laws():
    kb = KnowledgeBase()
    kb.load()
    general = kb.search("设备采购付款验收质量责任", top_k=20, kb_type="legal_kb",
                        filters={"contract_type": "general"}, use_vector=False)
    assert general
    # Procurement queries may use the dedicated sale-contract interpretation, but
    # unrelated specialist material must remain gated.
    assert {r["chunk"]["applicability_scope"] for r in general} <= {"general", "sale"}
    assert any(r["chunk"]["applicability_scope"] == "sale" for r in general)

    medical = kb.search("医疗器械注册证经营质量管理", top_k=20, kb_type="legal_kb",
                        filters={"contract_type": "medical_device_general"}, use_vector=False)
    assert any(r["chunk"]["applicability_scope"] == "medical_device" for r in medical)

    privacy = kb.search("委托处理个人信息和敏感信息", top_k=20, kb_type="legal_kb",
                        filters={"contract_type": "general"}, use_vector=False)
    assert any(r["chunk"]["applicability_scope"] == "data_privacy" for r in privacy)

    ordinary = kb.search("进入现场不得携带易燃易爆物品", top_k=30, kb_type="legal_kb",
                         filters={"contract_type": "general"}, use_vector=False)
    assert all(r["chunk"]["applicability_scope"] != "transport" for r in ordinary)
    transport = kb.search("托运人委托承运人运输货物并支付运费", top_k=30, kb_type="legal_kb",
                          filters={"contract_type": "transport"}, use_vector=False)
    assert any(r["chunk"]["applicability_scope"] == "transport" for r in transport)

    ordinary_ids = {r["chunk"]["knowledge_id"] for r in kb.search(
        "普通设备采购知识产权保证", top_k=100, kb_type="legal_kb",
        filters={"contract_type": "general"}, use_vector=False)}
    assert "civil_876" not in ordinary_ids
    development = kb.search("技术开发失败风险如何分担", top_k=50, kb_type="legal_kb",
                            filters={"contract_type": "technology_development"}, use_vector=False)
    development_ids = {r["chunk"]["knowledge_id"] for r in development}
    assert "civil_858" in development_ids
    assert "civil_885" not in development_ids


def test_unapproved_company_and_template_examples_are_not_published():
    domains = get_records_by_domain()
    for domain in ("company_kb", "template_kb"):
        assert all(r["status"] == "draft" and r["review_status"] != "published"
                   for r in domains[domain])
