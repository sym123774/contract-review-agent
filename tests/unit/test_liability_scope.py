from core.clause_checks import check_structured_clauses


def test_absolute_product_quality_exemption_is_flagged_conservatively():
    parsed = {"clauses": [{"clause_id":"c011", "title":"违约责任",
                            "content":"任何情况下，乙方对于设备质量问题、故障造成的损失不承担任何赔偿责任。"}]}
    risks = check_structured_clauses(parsed)
    found = [risk for risk in risks if risk.risk_id == "r_liability_scope_broad"]
    assert len(found) == 1
    assert "可能覆盖" in found[0].description
    assert "具体无效范围仍需" in found[0].description
    assert "第五百零六条" in found[0].legal_basis
