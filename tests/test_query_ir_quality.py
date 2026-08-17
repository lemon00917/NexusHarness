from microharness.medical.query_ir import build_query_ir
from microharness.medical.query_ir_quality import assess_query_ir, build_ir_ambiguity_response
from microharness.ollama.prompt_adapter import build_query_understanding_prompt


class _Profile:
    thinking = "none"


def _quality(condition: str, analysis: dict | None = None):
    analysis = analysis or {"type": "simple", "conditions": [{"text": condition}]}
    query_ir = build_query_ir(analysis, condition)
    return query_ir, assess_query_ir(query_ir, condition, analysis)


def test_complete_compound_query_ir_passes_quality_gate():
    condition = "\u672f\u524d24\u5c0f\u65f6\u4f7f\u7528\u8fc7\u963f\u53f8\u5339\u6797\u4e14\u672f\u524d48\u5c0f\u65f6\u5185\u4e2d\u6027\u7c92\u7ec6\u80de\u6570>1.5\u00d710\u2079/L"
    analysis = {
        "type": "compound",
        "connector": "and",
        "conditions": [
            {"text": "\u672f\u524d24\u5c0f\u65f6\u4f7f\u7528\u8fc7\u963f\u53f8\u5339\u6797", "domain": "medication"},
            {"text": "\u672f\u524d48\u5c0f\u65f6\u5185\u4e2d\u6027\u7c92\u7ec6\u80de\u6570>1.5\u00d710\u2079/L", "domain": "laboratory"},
        ],
    }
    query_ir, quality = _quality(condition, analysis)
    assert quality.valid
    assert not quality.issues
    assert [item.domain for item in query_ir.conditions] == ["medication", "laboratory"]


def test_unanchored_bounded_window_is_blocking():
    _, quality = _quality("24\u5c0f\u65f6\u5185\u4f7f\u7528\u8fc7\u836f\u7269")
    assert not quality.valid
    assert "TEMPORAL_ANCHOR_MISSING" in {item.code for item in quality.issues}


def test_incomplete_supplied_temporal_does_not_bypass_anchor_gate():
    condition = "24\u5c0f\u65f6\u5185\u4f7f\u7528\u8fc7\u836f\u7269"
    analysis = {
        "type": "simple",
        "conditions": [
            {
                "text": condition,
                "temporal": {"scope": "relative", "duration": 24, "unit": "hour"},
            }
        ],
    }
    _, quality = _quality(condition, analysis)
    assert not quality.valid
    assert "TEMPORAL_ANCHOR_MISSING" in {item.code for item in quality.issues}


def test_custom_structured_event_anchor_is_allowed():
    condition = "\u7ed9\u836f\u540e24\u5c0f\u65f6\u5185\u51fa\u73b0\u76ae\u75b9"
    analysis = {
        "type": "simple",
        "conditions": [
            {
                "text": condition,
                "temporal": {
                    "scope": "event_window",
                    "event": "medication_administration",
                    "relation": "after",
                    "duration": 24,
                    "unit": "hour",
                },
            }
        ],
    }
    _, quality = _quality(condition, analysis)
    assert quality.valid


def test_recent_and_encounter_windows_are_not_treated_as_ambiguous():
    for condition in ("\u6700\u8fd124\u5c0f\u65f6\u5185\u4f7f\u7528\u8fc7\u836f\u7269", "\u4f4f\u9662\u671f\u95f4\u4f7f\u7528\u8fc7\u836f\u7269"):
        _, quality = _quality(condition)
        assert quality.valid, condition


def test_event_relation_without_duration_is_valid():
    query_ir, quality = _quality("\u672f\u524d\u4f7f\u7528\u8fc7\u836f\u7269")
    temporal = query_ir.conditions[0].temporal
    assert quality.valid
    assert temporal is not None
    assert temporal.event == "surgery"
    assert temporal.relation == "before"
    assert temporal.duration is None


def test_trailing_event_duration_is_parsed():
    query_ir, quality = _quality("\u624b\u672f3\u5929\u540e\u4f7f\u7528\u8fc7\u836f\u7269")
    temporal = query_ir.conditions[0].temporal
    assert quality.valid
    assert temporal is not None
    assert temporal.event == "surgery"
    assert temporal.relation == "after"
    assert temporal.duration == 3.0
    assert temporal.unit == "\u5929"


def test_discharge_window_is_valid_and_not_treated_as_numeric_threshold():
    query_ir, quality = _quality("出院20天内血红蛋白指标异常")
    condition = query_ir.conditions[0]

    assert quality.valid
    assert condition.temporal is not None
    assert condition.temporal.event == "discharge"
    assert condition.temporal.relation == "after"
    assert condition.temporal.duration == 20.0
    assert condition.numeric_comparison is None


def test_surgery_window_without_direction_remains_ambiguous():
    _, quality = _quality("手术20天内出现症状")

    assert not quality.valid
    assert "TEMPORAL_ANCHOR_MISSING" in {item.code for item in quality.issues}


def test_open_clinical_concepts_are_not_blocked():
    for condition in ("\u70e7\u4f24", "\u80cc\u75db"):
        _, quality = _quality(condition)
        assert quality.valid, condition
        assert not quality.issues


def test_ambiguity_response_uses_unknown_contract():
    condition = "24\u5c0f\u65f6\u5185\u4f7f\u7528\u8fc7\u836f\u7269"
    query_ir, quality = _quality(condition)
    response = build_ir_ambiguity_response(condition, query_ir, quality, retried=True)
    assert response["\u5224\u65ad\u72b6\u6001"] == "\u65e0\u6cd5\u5224\u65ad"
    assert response["\u53ef\u5224\u5b9a"] is False
    assert response["error_code"] == "AMBIGUOUS_QUERY_IR"
    assert response["results"][0]["error_code"] == "AMBIGUOUS_QUERY_IR"
    assert response["ir_quality"]["retried"] is True


def test_retry_prompt_contains_quality_feedback():
    feedback = "- TEMPORAL_ANCHOR_MISSING: missing anchor"
    prompt = build_query_understanding_prompt(
        _Profile(),
        "24\u5c0f\u65f6\u5185\u4f7f\u7528\u8fc7\u836f\u7269",
        {},
        [],
        retry_feedback=feedback,
    )
    assert feedback in prompt
    assert "TEMPORAL_ANCHOR_MISSING" in prompt


def test_medical_query_retries_once_then_stops_before_scheduler(monkeypatch):
    from microharness.agent import query_normalizer, query_understanding
    from microharness.agent.scheduler import planner
    from microharness.medical import query_router
    from web.app import _run_medical_query

    condition = "24\u5c0f\u65f6\u5185\u4f7f\u7528\u8fc7\u836f\u7269"

    class Normalization:
        normalized = condition
        source = "test"
        confidence = 1.0

        def to_dict(self):
            return {"source": self.source, "confidence": self.confidence}

    calls = []

    def ambiguous_understanding(condition, model=None, document_catalog=None, retry_feedback=""):
        calls.append(retry_feedback)
        return {
            "type": "simple",
            "connector": None,
            "conditions": [{"text": condition, "domain": "medication"}],
            "source": "test",
        }

    class PlannerMustNotRun:
        def __init__(self, *args, **kwargs):
            raise AssertionError("scheduler ran after IR quality rejection")

    monkeypatch.setattr(query_normalizer, "normalize_query", lambda *args, **kwargs: Normalization())
    monkeypatch.setattr(query_understanding, "understand_query", ambiguous_understanding)
    monkeypatch.setattr(query_router, "reload_document_catalog_snapshot", lambda: ({}, {}))
    monkeypatch.setattr(query_router, "format_catalog_source_log", lambda *args, **kwargs: "catalog=test")
    monkeypatch.setattr(planner, "QueryPlanner", PlannerMustNotRun)

    response = _run_medical_query(
        condition,
        "0000000120",
        "174",
        "00001_120",
        "00001_174",
        "test-model",
        "test-model",
        None,
    )
    assert len(calls) == 2
    assert calls[0] == ""
    assert "TEMPORAL_ANCHOR_MISSING" in calls[1]
    assert response["\u5224\u65ad\u72b6\u6001"] == "\u65e0\u6cd5\u5224\u65ad"
    assert response["error_code"] == "AMBIGUOUS_QUERY_IR"
