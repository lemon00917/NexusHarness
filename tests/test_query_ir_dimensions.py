from microharness.medical.query_ir import build_query_ir
from microharness.medical.query_structure import repair_analysis_structure
from microharness.agent.query_understanding import _validate_and_normalize
from microharness.ollama.prompt_adapter import build_query_understanding_prompt


class _Profile:
    def __init__(self, thinking: str):
        self.thinking = thinking


def _build_condition(text: str, **overrides):
    condition = {"text": text, "keyword": text, **overrides}
    return build_query_ir(
        {"type": "simple", "connector": None, "conditions": [condition]},
        text,
    ).conditions[0]


def test_medication_condition_keeps_surgery_event_window():
    condition = _build_condition(
        "术前24小时使用过阿司匹林",
        entity_type="drug",
        target_skills=["drug-interaction"],
    )

    assert condition.condition_id == "c1"
    assert condition.domain == "medication"
    assert condition.temporal is not None
    assert condition.temporal.scope == "event_window"
    assert condition.temporal.event == "surgery"
    assert condition.temporal.relation == "before"
    assert condition.temporal.duration == 24.0
    assert condition.temporal.unit == "小时"
    assert condition.depends_on == ["event:surgery"]


def test_encounter_scoped_lab_condition_has_assertion():
    condition = _build_condition(
        "住院期间血红蛋白异常",
        entity_type="lab",
        predicate="abnormal",
        target_skills=["lab-results", "encounter-info"],
    )

    assert condition.domain == "laboratory"
    assert condition.temporal is not None
    assert condition.temporal.scope == "encounter"
    assert condition.temporal.relation == "during"
    assert condition.assertion is not None
    assert condition.assertion.present is True
    assert condition.assertion.temporal_context == "current"


def test_age_condition_is_demographic_numeric_comparison():
    condition = _build_condition("40岁以上", keyword="年龄")

    assert condition.domain == "demographic"
    assert condition.is_numeric is True
    assert condition.numeric_comparison == {
        "subject": "年龄",
        "operator": "以上",
        "threshold": 40.0,
        "unit": "岁",
    }


def test_length_of_stay_condition_is_encounter_dimension():
    condition = _build_condition(
        "住院天数小于5天",
        keyword="住院天数",
        target_skills=["encounter-info"],
    )

    assert condition.domain == "encounter"
    assert condition.numeric_comparison["threshold"] == 5.0
    assert condition.numeric_comparison["unit"] == "天"


def test_discharge_window_without_explicit_after_is_temporal_not_numeric():
    condition = _build_condition(
        "出院20天内血红蛋白指标异常",
        entity_type="lab",
        target_skills=["lab-results", "encounter-info"],
    )

    assert condition.temporal is not None
    assert condition.temporal.scope == "event_window"
    assert condition.temporal.event == "discharge"
    assert condition.temporal.relation == "after"
    assert condition.temporal.duration == 20.0
    assert condition.temporal.unit == "天"
    assert condition.is_numeric is False
    assert condition.numeric_comparison is None


def test_admission_window_without_explicit_after_is_normalized():
    condition = _build_condition("入院7天内出现症状")

    assert condition.temporal is not None
    assert condition.temporal.event == "admission"
    assert condition.temporal.relation == "after"
    assert condition.temporal.duration == 7.0
    assert condition.temporal.unit == "天"


def test_surgery_window_without_direction_is_not_guessed():
    condition = _build_condition("手术20天内出现症状")

    assert condition.temporal is None


def test_repeated_lab_condition_has_quantifier():
    condition = _build_condition(
        "至少两次白细胞异常",
        entity_type="lab",
        target_skills=["lab-results"],
    )

    assert condition.quantifier is not None
    assert condition.quantifier.mode == "at_least"
    assert condition.quantifier.count == 2.0
    assert condition.quantifier.unit == "次"


def test_all_records_condition_has_all_quantifier():
    condition = _build_condition(
        "所有白细胞记录均高于1.5×10^9/L",
        entity_type="lab",
        target_skills=["lab-results"],
    )

    assert condition.quantifier is not None
    assert condition.quantifier.mode == "all"


def test_at_most_count_condition_has_bounded_quantifier():
    condition = _build_condition(
        "至多两次白细胞异常",
        entity_type="lab",
        target_skills=["lab-results"],
    )

    assert condition.quantifier is not None
    assert condition.quantifier.mode == "at_most"
    assert condition.quantifier.count == 2.0


def test_latest_and_earliest_selection_are_not_parsed_as_exact_once():
    latest = _build_condition("最新一次血红蛋白异常", entity_type="lab")
    earliest = _build_condition("首次血红蛋白异常", entity_type="lab")

    assert latest.quantifier is not None
    assert latest.quantifier.mode == "latest"
    assert earliest.quantifier is not None
    assert earliest.quantifier.mode == "earliest"


def test_consecutive_count_preserves_sequence_semantics():
    condition = _build_condition("连续三次白细胞异常", entity_type="lab")

    assert condition.quantifier is not None
    assert condition.quantifier.mode == "consecutive"
    assert condition.quantifier.count == 3.0


def test_structured_llm_dimensions_take_priority():
    condition = _build_condition(
        "最近一段时间相关记录",
        domain="diagnosis",
        temporal={
            "scope": "event_window",
            "event": "custom_event",
            "relation": "after",
            "duration": "两",
            "unit": "天",
            "selection": "last",
        },
        assertion={
            "present": "false",
            "certainty": "suspected",
            "subject": "family",
            "temporal_context": "history",
        },
        quantifier={"mode": "at_least", "count": "三", "unit": "次"},
        depends_on=["condition:c0"],
        attributes={"source_hint": "structured"},
    )

    assert condition.domain == "diagnosis"
    assert condition.temporal.event == "custom_event"
    assert condition.temporal.duration == 2.0
    assert condition.assertion.present is False
    assert condition.assertion.certainty == "suspected"
    assert condition.quantifier.count == 3.0
    assert condition.depends_on == ["condition:c0", "event:custom_event"]
    assert condition.attributes == {"source_hint": "structured"}


def test_empty_analysis_still_builds_complete_condition():
    query_ir = build_query_ir({}, "既往否认胸痛")
    condition = query_ir.conditions[0]

    assert condition.condition_id == "c1"
    assert condition.domain == "clinical_concept"
    assert condition.assertion.present is False
    assert condition.assertion.temporal_context == "history"


def test_query_understanding_prompt_requests_multidimensional_ir():
    for thinking in ("native", "none"):
        prompt = build_query_understanding_prompt(
            _Profile(thinking),
            "术前24小时使用过某药",
            {},
            [],
        )

        for field in ("domain", "temporal", "assertion", "quantifier", "depends_on", "attributes"):
            assert field in prompt
        for field in ("outcome_state", "outcome_phase", "outcome_evidence"):
            assert field in prompt


def test_modern_ir_does_not_infer_missing_assertion_from_condition_text():
    condition = build_query_ir(
        {
            "source": "understand_query",
            "conditions": [{"text": "既往否认胸痛", "canonical_entity": "胸痛"}],
        },
        "既往否认胸痛",
    ).conditions[0]

    assert condition.assertion is not None
    assert condition.assertion.present is None
    assert condition.assertion.temporal_context == ""


def test_query_understanding_normalizer_preserves_structured_dimensions():
    result = _validate_and_normalize(
        {
            "conditions": [
                {
                    "text": "术前24小时使用过某药",
                    "domain": "medication",
                    "temporal": {"event": "surgery", "relation": "before"},
                    "assertion": {"present": True},
                    "quantifier": {"mode": "any", "count": 1},
                    "depends_on": ["event:surgery"],
                    "attributes": {"route": "口服"},
                }
            ]
        },
        "术前24小时使用过某药",
        {},
    )
    condition = result["conditions"][0]

    assert condition["domain"] == "medication"
    assert condition["temporal"]["event"] == "surgery"
    assert condition["assertion"]["present"] is True
    assert condition["quantifier"]["mode"] == "any"
    assert condition["depends_on"] == ["event:surgery"]
    assert condition["attributes"] == {"route": "口服"}


def test_query_understanding_preserves_unknown_documents_for_evidence_diagnostics():
    result = _validate_and_normalize(
        {
            "conditions": [
                {
                    "text": "相关病历条件",
                    "target_docs": ["入院记录", "未知文档", "未知文档"],
                }
            ]
        },
        "相关病历条件",
        {"入院记录": {"sections": {"主诉": {"field": "chief_complaint"}}}},
    )
    condition = result["conditions"][0]

    assert condition["target_docs"] == ["入院记录", "未知文档"]
    assert condition["unresolved_target_docs"] == ["未知文档"]


def test_query_understanding_normalizer_recovers_from_invalid_dimension_types():
    result = _validate_and_normalize(
        {
            "conditions": [
                {
                    "text": "相关记录",
                    "temporal": "bad",
                    "assertion": [],
                    "quantifier": 2,
                    "depends_on": "event:surgery",
                    "attributes": [],
                }
            ]
        },
        "相关记录",
        {},
    )
    condition = result["conditions"][0]

    assert condition["temporal"] is None
    assert condition["assertion"] is None
    assert condition["quantifier"] is None
    assert condition["depends_on"] == []
    assert condition["attributes"] == {}


def test_structure_repair_recovers_from_low_quality_compound_llm_output():
    text = "术前24小时使用过阿司匹林且术前48小时内中性粒细胞数大于1.5×10⁹/L的患者"
    low_quality_analysis = {
        "type": "simple",
        "connector": None,
        "conditions": [
            {
                "text": text,
                "keyword": text,
                "entity": text,
                "entity_type": "unknown",
                "domain": "diagnosis",
                "predicate": "unknown",
                "target_docs": [],
                "target_sections": [],
                "target_skills": [],
            }
        ],
        "source": "understand_query",
    }

    repaired = repair_analysis_structure(low_quality_analysis, text)
    query_ir = build_query_ir(repaired, text)

    assert repaired["type"] == "compound"
    assert repaired["connector"] == "and"
    assert [condition.domain for condition in query_ir.conditions] == [
        "medication",
        "laboratory",
    ]
    assert [condition.temporal.duration for condition in query_ir.conditions] == [
        24.0,
        48.0,
    ]
