from dataclasses import FrozenInstanceError

import pytest

from microharness.medical.condition_execution import (
    build_condition_execution_specs,
    prejudge_numeric_hints,
)
from microharness.medical.query_ir import build_query_ir


def test_modern_ir_preserves_canonical_entity_without_keyword_fallback():
    analysis = {
        "source": "understand_query+entity_normalize",
        "conditions": [{
            "condition_id": "c1",
            "text": "患者发烧",
            "keyword": "患者发烧",
            "entity": "发烧",
            "canonical_entity": "发热",
            "aliases": ["发烧"],
            "entity_type": "diagnosis",
            "target_skills": ["diagnosis-query"],
        }],
    }
    query_ir = build_query_ir(analysis, "患者发烧")

    specs = build_condition_execution_specs(
        query_ir,
        analysis,
        fallback_keyword_fn=lambda _: (_ for _ in ()).throw(
            AssertionError("modern IR must not invoke the legacy keyword extractor")
        ),
    )

    assert len(specs) == 1
    assert specs[0].keyword == "患者发烧"
    assert specs[0].canonical_entity == "发热"
    assert specs[0].entity_candidates == ("发热", "发烧")
    assert specs[0].execution_source == "ir"
    assert specs[0].legacy_fallback_allowed is False


def test_legacy_ir_invokes_keyword_fallback():
    calls = []
    analysis = {
        "source": "fallback+entity_normalize",
        "conditions": [{"text": "患者发烧", "keyword": "患者发烧"}],
    }
    query_ir = build_query_ir(analysis, "患者发烧")

    specs = build_condition_execution_specs(
        query_ir,
        analysis,
        fallback_keyword_fn=lambda value: calls.append(value) or "发烧",
    )

    assert calls == ["患者发烧"]
    assert specs[0].keyword == "发烧"
    assert specs[0].canonical_entity == "发烧"
    assert specs[0].entity_candidates[0] == "发烧"
    assert specs[0].execution_source == "legacy_fallback"
    assert specs[0].legacy_fallback_allowed is True


def test_duplicate_condition_text_has_position_stable_execution_keys():
    analysis = {
        "source": "understand_query",
        "conditions": [
            {"condition_id": "same", "text": "背痛"},
            {"condition_id": "same", "text": "背痛"},
        ],
    }

    specs = build_condition_execution_specs(
        build_query_ir(analysis, "背痛并且背痛"),
        analysis,
    )

    assert [spec.text for spec in specs] == ["背痛", "背痛"]
    assert [spec.execution_key for spec in specs] == ["same@1", "same@2"]
    assert len({spec.execution_key for spec in specs}) == 2


def test_evidence_plan_targets_and_source_ids_are_preserved():
    analysis = {
        "source": "understand_query+evidence_plan",
        "conditions": [{
            "condition_id": "c1",
            "text": "背痛",
            "target_docs": ["入院记录"],
            "target_sections": ["主诉", "现病史"],
            "targets": {"入院记录": ["主诉", "现病史"]},
            "evidence_plan_source_ids": ["c1:document:入院记录"],
        }],
    }

    spec = build_condition_execution_specs(
        build_query_ir(analysis, "背痛"),
        analysis,
    )[0]

    assert spec.targets_dict() == {"入院记录": ["主诉", "现病史"]}
    assert spec.evidence_plan_source_ids == ("c1:document:入院记录",)


@pytest.mark.parametrize(
    ("condition", "expected_docs", "expected_services"),
    [
        ({"text": "血红蛋白偏低", "entity_type": "lab", "target_skills": ["lab-results"]}, (), ("lab-results",)),
        ({"text": "背痛", "entity_type": "diagnosis", "target_docs": ["入院记录", "出院记录"], "target_sections": ["现病史", "出院诊断"]}, ("入院记录", "出院记录"), ()),
    ],
)
def test_pure_route_dimensions_survive_execution_spec(condition, expected_docs, expected_services):
    analysis = {"source": "understand_query", "conditions": [condition]}
    spec = build_condition_execution_specs(
        build_query_ir(analysis, condition["text"]),
        analysis,
    )[0]

    assert spec.target_docs == expected_docs
    assert spec.target_services == expected_services


def test_condition_dict_carries_complete_ir_dimensions_and_spec_is_frozen():
    analysis = {
        "source": "understand_query",
        "conditions": [{
            "condition_id": "c1",
            "text": "术前24小时至少使用2次阿司匹林",
            "canonical_entity": "阿司匹林",
            "temporal": {
                "scope": "event_window",
                "event": "surgery",
                "relation": "before",
                "duration": 24,
                "unit": "小时",
            },
            "assertion": {
                "present": True,
                "certainty": "confirmed",
                "subject": "patient",
                "temporal_context": "current",
            },
            "quantifier": {"mode": "at_least", "count": 2, "unit": "次"},
            "depends_on": ["event:surgery"],
            "attributes": {"route_reason": "explicit_ir"},
        }],
    }
    spec = build_condition_execution_specs(
        build_query_ir(analysis, analysis["conditions"][0]["text"]),
        analysis,
    )[0]

    compat = spec.condition_dict()
    assert compat["temporal"]["event"] == "surgery"
    assert compat["assertion"]["subject"] == "patient"
    assert compat["quantifier"] == {"mode": "at_least", "count": 2.0, "unit": "次"}
    assert compat["depends_on"] == ["event:surgery"]
    assert compat["attributes"] == {"route_reason": "explicit_ir"}
    with pytest.raises(FrozenInstanceError):
        spec.keyword = "changed"


def test_modern_ir_uses_structured_outcome_semantics_without_text_parsing(monkeypatch):
    import microharness.medical.condition_execution as condition_execution

    def fail_legacy_parser(*args, **kwargs):
        raise AssertionError("modern IR must not invoke a legacy text parser")

    monkeypatch.setattr(condition_execution, "extract_outcome_modifiers", fail_legacy_parser)
    monkeypatch.setattr(condition_execution, "extract_outcome_keyword", fail_legacy_parser)
    monkeypatch.setattr(condition_execution, "extract_outcome_phase", fail_legacy_parser)
    monkeypatch.setattr(condition_execution, "is_pre_admission_condition", fail_legacy_parser)

    analysis = {
        "source": "understand_query+entity_normalize",
        "conditions": [{
            "condition_id": "c1",
            "text": "这段原文不参与执行语义解析",
            "entity": "背痛",
            "canonical_entity": "背痛",
            "entity_type": "outcome",
            "predicate": "outcome",
            "assertion": {
                "present": True,
                "certainty": "confirmed",
                "subject": "patient",
                "temporal_context": "current",
            },
            "attributes": {
                "outcome_state": "improved",
                "outcome_phase": "discharge",
                "outcome_evidence": "state",
            },
        }],
    }

    spec = build_condition_execution_specs(
        build_query_ir(analysis, analysis["conditions"][0]["text"]),
        analysis,
    )[0]

    assert spec.canonical_entity == "背痛"
    assert spec.outcome_state == "improved"
    assert spec.outcome_phase == "discharge"
    assert spec.is_outcome_condition is True
    assert spec.history_context is False
    assert spec.internal_negation is False
    assert spec.diagnosis_phase_evidence_allowed is False


def test_modern_ir_uses_structured_history_and_negation_assertion():
    analysis = {
        "source": "understand_query",
        "conditions": [{
            "text": "原句不包含历史或否定词",
            "canonical_entity": "高血压",
            "assertion": {
                "present": False,
                "certainty": "confirmed",
                "subject": "patient",
                "temporal_context": "history",
            },
        }],
    }

    spec = build_condition_execution_specs(
        build_query_ir(analysis, analysis["conditions"][0]["text"]),
        analysis,
    )[0]

    assert spec.history_context is True
    assert spec.internal_negation is True


def test_structured_not_improved_state_counts_as_internal_negation():
    analysis = {
        "source": "understand_query",
        "negated": True,
        "conditions": [{
            "text": "展示文本不含否定词",
            "canonical_entity": "背痛",
            "predicate": "outcome",
            "assertion": {
                "present": True,
                "certainty": "confirmed",
                "subject": "patient",
                "temporal_context": "current",
            },
            "attributes": {"outcome_state": "not_improved"},
        }],
    }

    spec = build_condition_execution_specs(
        build_query_ir(analysis, analysis["conditions"][0]["text"]),
        analysis,
    )[0]

    assert spec.outcome_state == "not_improved"
    assert spec.internal_negation is True


def test_modern_ir_allows_discharge_diagnosis_only_when_explicitly_structured():
    analysis = {
        "source": "understand_query",
        "conditions": [{
            "text": "无关展示文本",
            "canonical_entity": "背痛",
            "predicate": "diagnosed",
            "attributes": {
                "outcome_phase": "discharge",
                "outcome_evidence": "diagnosis",
            },
        }],
    }

    spec = build_condition_execution_specs(
        build_query_ir(analysis, analysis["conditions"][0]["text"]),
        analysis,
    )[0]

    assert spec.outcome_phase == "discharge"
    assert spec.diagnosis_phase_evidence_allowed is True


def test_legacy_fallback_extracts_outcome_semantics_once():
    analysis = {
        "source": "fallback+entity_normalize",
        "conditions": [{
            "text": "出院时背痛好转的患者",
            "keyword": "出院时背痛好转的患者",
        }],
    }

    spec = build_condition_execution_specs(
        build_query_ir(analysis, analysis["conditions"][0]["text"]),
        analysis,
        fallback_keyword_fn=lambda value: value,
    )[0]

    assert spec.canonical_entity == "背痛"
    assert spec.modifiers == ("好转",)
    assert spec.outcome_state == "improved"
    assert spec.outcome_phase == "discharge"
    assert spec.is_outcome_condition is True
    assert spec.execution_source == "legacy_fallback"


def test_structured_age_comparison_uses_ir_with_irrelevant_display_text(monkeypatch):
    import microharness.medical.query_ir as query_ir_module

    monkeypatch.setattr(
        query_ir_module,
        "parse_numeric_comparison",
        lambda *_: (_ for _ in ()).throw(
            AssertionError("modern structured comparison must not parse display text")
        ),
    )
    analysis = {
        "source": "understand_query",
        "conditions": [{
            "text": "这段文本只用于展示",
            "domain": "demographic",
            "entity_type": "age",
            "predicate": "compare",
            "keyword": "年龄",
            "is_numeric": True,
            "numeric_comparison": {
                "subject": "年龄",
                "operator": ">=",
                "threshold": 40,
                "unit": "岁",
            },
        }],
    }

    spec = build_condition_execution_specs(
        build_query_ir(analysis, analysis["conditions"][0]["text"]),
        analysis,
    )[0]
    result = prejudge_numeric_hints(spec, "[预计算] 年龄 = 45岁")

    assert spec.is_age_condition is True
    assert spec.numeric_comparison_issue() == ""
    assert result is not None
    assert result["matched"] is True


def test_structured_duration_comparison_uses_ir_without_raw_text_parsing():
    analysis = {
        "source": "understand_query",
        "conditions": [{
            "text": "无关展示文本",
            "domain": "encounter",
            "entity_type": "duration",
            "predicate": "compare",
            "keyword": "住院天数",
            "is_numeric": True,
            "numeric_comparison": {
                "subject": "住院天数",
                "operator": "<",
                "threshold": 5,
                "unit": "天",
            },
        }],
    }

    spec = build_condition_execution_specs(
        build_query_ir(analysis, analysis["conditions"][0]["text"]),
        analysis,
    )[0]
    result = prejudge_numeric_hints(
        spec,
        "[预计算] 出院日期时间 - 入院日期时间(天) = 2天",
    )

    assert spec.numeric_execution_required is True
    assert result is not None
    assert result["matched"] is True


@pytest.mark.parametrize(
    ("comparison", "expected_issue"),
    [
        ({"subject": "年龄", "operator": ">=", "unit": "岁"}, "缺少结构化数值比较条件"),
        ({"subject": "年龄", "operator": ">=", "threshold": 40}, "年龄比较缺少单位“岁”"),
        ({"subject": "年龄", "operator": "around", "threshold": 40, "unit": "岁"}, "数值比较缺少有效比较符"),
    ],
)
def test_incomplete_modern_numeric_ir_remains_unresolved(comparison, expected_issue):
    analysis = {
        "source": "understand_query",
        "conditions": [{
            "text": "展示文本不提供比较信息",
            "domain": "demographic",
            "entity_type": "age",
            "predicate": "compare",
            "keyword": "年龄",
            "is_numeric": True,
            "numeric_comparison": comparison,
        }],
    }

    spec = build_condition_execution_specs(
        build_query_ir(analysis, analysis["conditions"][0]["text"]),
        analysis,
    )[0]

    assert spec.numeric_comparison_issue() == expected_issue
    assert prejudge_numeric_hints(spec, "[预计算] 年龄 = 45岁") is None


def test_legacy_chinese_numeric_expression_still_executes():
    analysis = {
        "source": "fallback+entity_normalize",
        "conditions": [{
            "text": "住院天数小于5天",
            "keyword": "住院天数",
        }],
    }

    spec = build_condition_execution_specs(
        build_query_ir(analysis, analysis["conditions"][0]["text"]),
        analysis,
        fallback_keyword_fn=lambda value: value,
    )[0]
    result = prejudge_numeric_hints(
        spec,
        "[预计算] 出院日期时间 - 入院日期时间(天) = 2天",
    )

    assert spec.legacy_fallback_allowed is True
    assert spec.numeric_comparison["threshold"] == 5.0
    assert result is not None
    assert result["matched"] is True
