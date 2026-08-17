from microharness.medical.semantic_rules import augment_analysis_routes
from microharness.medical.query_structure import repair_analysis_structure


def _wrong_diagnosis_analysis(text: str) -> dict:
    return {
        "conditions": [
            {
                "text": text,
                "keyword": text,
                "entity": text,
                "entity_type": "diagnosis",
                "domain": "diagnosis",
                "predicate": "exists",
                "semantic_class": "疾病/症状存在",
                "target_skills": ["diagnosis-query"],
                "target_docs": ["出院记录"],
                "target_sections": ["入院诊断", "出院诊断"],
            }
        ]
    }


def _wrong_lab_analysis(text: str) -> dict:
    return {
        "conditions": [
            {
                "text": text,
                "keyword": text,
                "entity": text,
                "entity_type": "lab",
                "domain": "laboratory",
                "predicate": "compare",
                "semantic_class": "检验指标",
                "target_skills": ["lab-results"],
            }
        ]
    }


def test_inpatient_lab_condition_repairs_wrong_diagnosis_route():
    text = "住院期间高密度脂蛋白胆固醇指标偏低"

    condition = augment_analysis_routes(
        _wrong_diagnosis_analysis(text),
        text,
        fallback_keyword_fn=lambda _: "高密度脂蛋白胆固醇",
    )["conditions"][0]

    assert condition["entity_type"] == "lab"
    assert condition["domain"] == "laboratory"
    assert condition["predicate"] == "low"
    assert condition["semantic_class"] == "检验指标"
    assert condition["target_skills"] == ["lab-results", "encounter-info"]
    assert condition["target_docs"] == []
    assert condition["target_sections"] == []


def test_numeric_lab_condition_repairs_wrong_diagnosis_route():
    text = "术前48小时内白细胞>1.5x10⁹/L"

    condition = augment_analysis_routes(
        _wrong_diagnosis_analysis(text),
        text,
        fallback_keyword_fn=lambda _: "白细胞",
    )["conditions"][0]

    assert condition["entity_type"] == "lab"
    assert condition["domain"] == "laboratory"
    assert condition["predicate"] == "compare"
    assert condition["target_skills"] == ["lab-results"]
    assert "diagnosis-query" not in condition["target_skills"]


def test_explicit_disease_name_with_lab_term_stays_on_diagnosis_service():
    text = "患有白细胞减少症"
    analysis = _wrong_diagnosis_analysis(text)

    condition = augment_analysis_routes(analysis, text)["conditions"][0]

    assert condition["entity_type"] == "diagnosis"
    assert condition["domain"] == "diagnosis"
    assert condition["target_skills"] == ["diagnosis-query"]
    assert "lab-results" not in condition["target_skills"]


def test_age_comparison_overrides_wrong_lab_route():
    text = "40岁以上"

    condition = repair_analysis_structure(_wrong_lab_analysis(text), text)["conditions"][0]

    assert condition["entity_type"] == "age"
    assert condition["domain"] == "demographic"
    assert condition["target_skills"] == []


def test_inpatient_duration_overrides_wrong_lab_route():
    text = "住院天数小于5天"

    condition = repair_analysis_structure(_wrong_lab_analysis(text), text)["conditions"][0]

    assert condition["entity_type"] == "encounter"
    assert condition["domain"] == "encounter"
    assert condition["target_skills"] == ["encounter-info"]
