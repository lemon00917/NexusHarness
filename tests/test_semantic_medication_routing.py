from microharness.medical.semantic_rules import (
    augment_analysis_routes,
    has_explicit_medication_action,
    uses_deterministic_medication_pipeline,
)


def _fallback_keyword(text: str) -> str:
    if "阿司匹林" in text:
        return "阿司匹林"
    if "氯吡格雷" in text:
        return "氯吡格雷"
    return text


def test_explicit_ordered_medication_action_repairs_wrong_diagnosis_route():
    analysis = {
        "type": "compound",
        "connector": "and",
        "conditions": [
            {
                "text": "术前开过阿司匹林",
                "keyword": "阿司匹林",
                "entity": "阿司匹林",
                "entity_type": "diagnosis",
                "predicate": "exists",
                "semantic_class": "疾病/症状存在",
                "target_skills": ["diagnosis-query"],
                "target_docs": ["入院记录", "出院记录", "手术记录"],
                "target_sections": ["入院诊断", "手术日期"],
            },
            {
                "text": "术前48小时内中性粒细胞数>1.5×10⁹/L",
                "keyword": "中性粒细胞数",
                "entity_type": "lab",
                "target_skills": ["lab-results"],
            },
        ],
    }

    repaired = augment_analysis_routes(
        analysis,
        "术前开过阿司匹林且术前48小时内中性粒细胞数>1.5×10⁹/L的患者",
        fallback_keyword_fn=_fallback_keyword,
    )

    medication = repaired["conditions"][0]
    assert medication["entity_type"] == "drug"
    assert medication["predicate"] == "ordered"
    assert medication["semantic_class"] == "用药医嘱"
    assert medication["target_skills"] == ["drug-interaction"]
    assert "入院记录" not in medication["target_docs"]
    assert "出院记录" not in medication["target_docs"]
    assert "手术记录" in medication["target_docs"]
    assert "入院诊断" not in medication["target_sections"]
    assert "手术日期" in medication["target_sections"]


def test_explicit_ordered_medication_action_is_not_drug_name_specific():
    analysis = {
        "conditions": [{
            "text": "术前开立过氯吡格雷",
            "keyword": "氯吡格雷",
            "entity_type": "diagnosis",
            "predicate": "exists",
            "target_skills": ["diagnosis-query"],
        }]
    }

    medication = augment_analysis_routes(
        analysis,
        "术前开立过氯吡格雷",
        fallback_keyword_fn=_fallback_keyword,
    )["conditions"][0]

    assert medication["entity_type"] == "drug"
    assert medication["predicate"] == "ordered"
    assert medication["target_skills"] == ["drug-interaction"]


def test_medication_route_sets_domain_and_uses_unified_pipeline():
    analysis = {'conditions': [{
        'text': '入院120天内注射过氯吡格雷',
        'entity_type': 'diagnosis',
        'target_skills': ['diagnosis-query'],
    }]}
    medication = augment_analysis_routes(
        analysis, '入院120天内注射过氯吡格雷', fallback_keyword_fn=_fallback_keyword,
    )['conditions'][0]
    assert medication['domain'] == 'medication'
    assert medication['target_skills'] == ['encounter-info', 'drug-interaction']
    assert uses_deterministic_medication_pipeline(analysis) is True


def test_non_medication_temporal_analysis_keeps_existing_pipeline():
    analysis = {'conditions': [{
        'text': '出院后10天内血红蛋白异常',
        'entity_type': 'lab',
        'domain': 'laboratory',
        'target_skills': ['lab-results'],
    }]}
    assert uses_deterministic_medication_pipeline(analysis) is False


def test_opened_surgery_is_not_treated_as_medication_action():
    assert has_explicit_medication_action("既往开过手术") is False
    assert has_explicit_medication_action("曾经开过一次手术") is False
    assert has_explicit_medication_action("开过刀") is False
