import re

from knowledge_base.legal_data import get_records


def _records():
    return {record["knowledge_id"]: record for record in get_records()}


def test_civil_code_contract_part_is_complete_and_versioned():
    records = _records()
    expected = {f"civil_{number}" for number in range(463, 989)}
    assert expected <= records.keys()
    for knowledge_id in expected:
        record = records[knowledge_id]
        assert record["source"] == "中华人民共和国民法典"
        assert re.fullmatch(r"第\d+条", record["article_or_clause"])
        assert record["effective_from"] == "2021-01-01"


def test_core_interpretations_are_complete_and_clean():
    records = _records()
    guarantee = [records[f"interp_guarantee_{number}"] for number in range(1, 72)]
    sale = [records[f"interp_sale_{number}"] for number in range(1, 34)]
    assert guarantee[0]["content"].startswith("因抵押、质押、留置、保证等担保发生的纠纷")
    assert guarantee[-1]["content"] == "本解释自2021年1月1日起施行。"
    assert sale[0]["content"].startswith("当事人之间没有书面合同")
    assert "民法典第六百二十一条" in sale[13]["content"]
    assert all("上一篇：" not in record["content"] for record in sale)
    assert all(not re.search(r"[一二三四]+、关于", record["content"]) for record in guarantee)
    assert all(record["effective_from"] == "2021-01-01" for record in guarantee + sale)


def test_every_published_legal_record_has_traceable_version_metadata():
    for record in get_records():
        assert record["source"], record["knowledge_id"]
        assert record["article_or_clause"], record["knowledge_id"]
        assert record.get("effective_from") or record.get("effective_date"), record["knowledge_id"]
        assert record.get("version"), record["knowledge_id"]
        assert record.get("source_url"), record["knowledge_id"]


def test_electronic_arbitration_and_network_data_sources_are_complete():
    records = _records()
    assert all(f"esign_{number}" in records for number in range(1, 37))
    assert all(f"arbitration_{number}" in records for number in range(1, 97))
    assert all(f"network_data_{number}" in records for number in range(1, 65))
    assert records["esign_14"]["content"] == "可靠的电子签名与手写签名或者盖章具有同等的法律效力。"
    assert records["arbitration_27"]["effective_from"] == "2026-03-01"
    assert "通过合同等" in records["network_data_12"]["content"]


def test_current_technology_contract_interpretation_is_complete_and_scoped():
    records = _records()
    interpretation = [records[f"interp_tech_{number}"] for number in range(1, 48)]
    assert "民法典第八百四十七条" in interpretation[1]["content"]
    assert "适用民法典第三编第一分编" in interpretation[45]["content"]
    assert all(record["version"].startswith("2020年修正") for record in interpretation)
    assert all(record["applicability_scope"] == "technology_general" for record in interpretation)
