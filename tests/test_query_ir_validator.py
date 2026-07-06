from microharness.medical.query_ir_validator import validate_and_repair_analysis


def _kw(text: str) -> str:
    return text


def test_validator_rebuilds_mismatched_compound_conditions():
    original = "住院天数大于三天并且术后24小时内开了药"
    analysis = {
        "type": "simple",
        "connector": None,
        "conditions": [{"text": "住院天数大于三天", "keyword": "住院天数"}],
    }

    repaired = validate_and_repair_analysis(analysis, original, fallback_keyword_fn=_kw)

    assert repaired["type"] == "compound"
    assert repaired["connector"] == "and"
    assert [c["text"] for c in repaired["conditions"]] == ["住院天数大于三天", "术后24小时内开了药"]
    assert not repaired["ir_validation"]["valid"]


def test_validator_preserves_temporal_clause_text():
    original = "术后24小时内开了药"
    analysis = {"conditions": [{"text": "开了药", "keyword": "药"}]}

    repaired = validate_and_repair_analysis(analysis, original, fallback_keyword_fn=_kw)

    assert repaired["conditions"][0]["text"] == original
    assert any(i["code"] == "temporal_expression_missing" for i in repaired["ir_validation"]["issues"])


def test_validator_preserves_numeric_unit_clause_text():
    original = "术前48小时内指标>1.5x10⁹/L"
    analysis = {"conditions": [{"text": "术前48小时内指标>1.5", "keyword": "指标"}]}

    repaired = validate_and_repair_analysis(analysis, original, fallback_keyword_fn=_kw)

    assert repaired["conditions"][0]["text"] == original
    issue_codes = {i["code"] for i in repaired["ir_validation"]["issues"]}
    assert "numeric_threshold_changed" in issue_codes
    assert "unit_missing" in issue_codes

