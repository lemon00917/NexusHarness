from microharness.agent.query_understanding import _validate_and_normalize
from microharness.medical.diagnosis_rules import judge_diagnosis_condition
from microharness.medical.entity_normalization import entity_candidates, normalize_entity_fields
from microharness.medical.lab_rules import judge_lab_condition
from microharness.medical.medication_rules import judge_medication_condition
from microharness.medical.query_ir import build_query_ir
from microharness.ollama.prompt_adapter import build_query_understanding_prompt


class _Profile:
    def __init__(self, thinking: str):
        self.thinking = thinking


def test_normalization_deduplicates_case_and_punctuation_variants():
    condition = {
        "entity": "Alpha",
        "canonical_entity": "Alpha",
        "aliases": ["alpha", "A-L-P-H-A", "Beta", "Beta"],
        "entity_confidence": 1.4,
        "normalization_source": "llm",
    }

    normalize_entity_fields(condition)

    assert condition["canonical_entity"] == "Alpha"
    assert condition["aliases"] == ["Beta"]
    assert condition["entity_candidates"] == ["Alpha", "Beta"]
    assert condition["entity_confidence"] == 1.0
    assert condition["normalization_source"] == "llm"


def test_invalid_confidence_is_ignored_and_fallback_is_deterministic():
    condition = {"entity": "Gamma", "entity_confidence": "unknown"}

    normalize_entity_fields(condition)

    assert condition.get("entity_confidence") is None
    assert condition["entity_candidates"] == ["Gamma"]
    assert condition["normalization_source"] == "deterministic"


def test_normalization_removes_query_subject_words_from_entity_fields():
    condition = {
        "entity": "患者发烧",
        "canonical_entity": "患者发烧",
        "keyword": "患者发烧",
    }

    normalize_entity_fields(condition)

    assert condition["canonical_entity"] == "发烧"
    assert condition["entity"] == "发烧"
    assert condition["keyword"] == "发烧"
    assert condition["entity_candidates"] == ["发烧"]


def test_query_understanding_and_ir_preserve_entity_normalization():
    analysis = _validate_and_normalize(
        {
            "type": "simple",
            "conditions": [
                {
                    "text": "存在原始实体",
                    "entity": "原始实体",
                    "canonical_entity": "规范实体",
                    "aliases": ["等价简称"],
                    "entity_confidence": 0.92,
                    "normalization_source": "llm",
                }
            ],
        },
        "存在原始实体",
        {},
    )
    condition = analysis["conditions"][0]
    query_ir = build_query_ir(analysis, "存在原始实体")
    serialized = query_ir.conditions[0].to_dict()

    assert condition["canonical_entity"] == "规范实体"
    assert condition["aliases"] == ["原始实体", "等价简称"]
    assert entity_candidates("", condition) == ["规范实体", "原始实体", "等价简称"]
    assert serialized["标准实体"] == "规范实体"
    assert serialized["实体别名"] == ["原始实体", "等价简称"]
    assert serialized["实体置信度"] == 0.92
    assert serialized["实体归一来源"] == "llm"


def test_prompt_requests_strict_equivalent_aliases_for_all_profiles():
    for thinking in ("native", "disabled"):
        prompt = build_query_understanding_prompt(_Profile(thinking), "查询实体", {}, [])
        assert "canonical_entity" in prompt
        assert "entity_confidence" in prompt
        assert "normalization_source" in prompt
        assert "严格等价" in prompt


def test_diagnosis_rule_matches_supplied_alias_and_reports_it():
    bindings = [
        {
            "html_field": "[诊断1] 诊断名称",
            "eng_field": "diagnoseName",
            "value": "等价诊断名",
        },
        {
            "html_field": "[诊断1] 诊断类型",
            "eng_field": "diagTypeDesc",
            "value": "出院诊断",
        },
    ]

    result = judge_diagnosis_condition(
        "患有规范诊断名",
        bindings,
        entity="规范诊断名",
        entity_candidates=["规范诊断名", "等价诊断名"],
        semantic={"domain": "diagnosis", "entity_type": "diagnosis"},
    )

    assert result["matched"] is True
    assert result["candidate_records"][0]["匹配实体"] == "等价诊断名"


def test_lab_rule_matches_supplied_abbreviation_and_reports_it():
    bindings = [
        {"html_field": "[检验1] 化验项目描述", "eng_field": "inspItemDesc", "value": "等价项目全称"},
        {"html_field": "[检验1] 缩写", "eng_field": "inspItemAbbr", "value": "TST"},
        {"html_field": "[检验1] 结果", "eng_field": "inspectionValue", "value": "9.0"},
        {"html_field": "[检验1] 异常标志", "eng_field": "inspAbnoFlag", "value": "H"},
        {"html_field": "[检验1] 参考范围", "eng_field": "inspResultRange", "value": "1-5"},
    ]

    result = judge_lab_condition(
        "规范检验项偏高",
        bindings,
        entity_candidates=["规范检验项", "TST"],
    )

    assert result["matched"] is True
    assert result["candidate_records"][0]["匹配实体"] == "TST"


def test_medication_rule_matches_supplied_alias_and_reports_it():
    bindings = [
        {"html_field": "[用药1] 药物名称", "eng_field": "orderName", "value": "等价药名"},
        {"html_field": "[用药1] 开立日期时间", "eng_field": "开立日期时间", "value": "2026-07-01 08:00:00"},
        {"html_field": "[用药1] 医嘱状态描述", "eng_field": "ordStatusDesc", "value": "执行"},
    ]
    semantic = {
        "domain": "medication",
        "entity_type": "drug",
        "predicate": "ordered",
        "fields": {
            "entity": ["orderName"],
            "ordered_at": ["开立日期时间"],
            "status": ["ordStatusDesc"],
        },
        "predicate_policies": {
            "ordered": {
                "event_time_role": "ordered_at",
                "required_status": True,
                "accepted_status_values": ["执行"],
            }
        },
    }

    result = judge_medication_condition(
        "开过规范药名",
        bindings,
        entity="规范药名",
        entity_candidates=["规范药名", "等价药名"],
        semantic=semantic,
    )

    assert result["matched"] is True
    assert result["candidate_records"][0]["匹配实体"] == "等价药名"
