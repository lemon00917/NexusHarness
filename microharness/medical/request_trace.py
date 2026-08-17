"""Request-level observability for medical-filter execution.

The trace is derived from canonical machine fields already produced by the
query pipeline. It is intentionally read-only: building a trace must never
change routing, evidence adjudication, or the public judgment.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Iterable

from microharness.medical.evidence import infer_evidence_uncertainty_kind

TRACE_SCHEMA_VERSION = "1.1.0"

_IR_QUALITY_KEY = "IR\u8d28\u91cf"
_EVIDENCE_PLAN_KEY = "\u8bc1\u636e\u8ba1\u5212"
_EXPLANATION_AUDIT_KEY = "\u89e3\u91ca\u6821\u9a8c"

_ISSUE_LAYER_ORDER = {
    "understanding": 10,
    "ir": 20,
    "routing": 30,
    "evidence": 40,
    "temporal": 50,
    "condition_adjudication": 60,
    "overall_adjudication": 70,
    "explanation": 80,
}

_QUALITY_PRIORITY = {
    "COMPLETE": 0,
    "PARTIAL": 1,
    "MISSING": 2,
    "SOURCE_ERROR": 3,
}

_CONFLICT_PRIORITY = {
    "NONE": 0,
    "SUPPORTING_DISAGREEMENT": 1,
    "CONCLUSIVE_CONFLICT": 2,
}

_TEMPORAL_ISSUE_CODES = {
    "MISSING_EVENT_TIME",
    "TEMPORAL_ANCHOR_MISSING",
    "TEMPORAL_EVENT_UNRESOLVED",
    "QUANTIFIER_RECORD_TIME_MISSING",
}


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _first_patient_result(response: dict[str, Any]) -> dict[str, Any]:
    for item in _list(response.get("results")):
        if isinstance(item, dict):
            return item
    return {}


def _canonical_conditions(response: dict[str, Any]) -> list[dict[str, Any]]:
    first = _first_patient_result(response)
    explicit = first.get("condition_results")
    if isinstance(explicit, list):
        return [item for item in explicit if isinstance(item, dict)]

    conditions = []
    for info in _dict(first.get("per_condition")).values():
        canonical = _dict(_dict(info).get("condition_result"))
        if canonical:
            conditions.append(canonical)
    return conditions


def _source_decisions(conditions: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    decisions = []
    for condition in conditions:
        decisions.extend(
            item
            for item in _list(condition.get("source_decisions"))
            if isinstance(item, dict)
        )
    return decisions


def _source_uncertainty_kind(source: dict[str, Any]) -> str:
    return infer_evidence_uncertainty_kind(
        status=source.get("status"),
        reason_code=source.get("reason_code"),
        data_quality=source.get("data_quality"),
        missing_capabilities=_list(source.get("missing_capabilities")),
        selection_complete=source.get("selection_complete"),
        semantic_trace=_list(source.get("semantic_trace")),
        explicit=source.get("uncertainty_kind"),
    ).value


def _explanation_audits(response: dict[str, Any]) -> list[dict[str, Any]]:
    first = _first_patient_result(response)
    audits = []

    overall = _dict(first.get(_EXPLANATION_AUDIT_KEY))
    if overall:
        audits.append(overall)

    for info in _dict(first.get("per_condition")).values():
        info = _dict(info)
        condition_audit = _dict(info.get(_EXPLANATION_AUDIT_KEY))
        if condition_audit:
            audits.append(condition_audit)
        for source in _list(info.get("files")):
            source_audit = _dict(_dict(source).get(_EXPLANATION_AUDIT_KEY))
            if source_audit:
                audits.append(source_audit)
    return audits


def _worst_value(values: Iterable[str], priority: dict[str, int], default: str) -> str:
    normalized = [str(value or "").upper() for value in values]
    normalized = [value for value in normalized if value in priority]
    if not normalized:
        return default
    return max(normalized, key=lambda value: priority[value])


def _queue_trace(admission: dict[str, Any] | None) -> dict[str, Any]:
    state = _dict(admission)
    submitted_at = state.get("submitted_at")
    started_at = state.get("started_at")
    wait_ms = 0
    if isinstance(submitted_at, (int, float)) and isinstance(started_at, (int, float)):
        wait_ms = max(0, int((started_at - submitted_at) * 1000))
    return {
        "wait_ms": wait_ms,
        "max_concurrency": int(state.get("max_concurrency") or 0),
        "active_count": int(state.get("active_count") or 0),
        "max_queue": int(state.get("max_queue") or 0),
        "queue_length": int(state.get("queue_length") or 0),
        "queue_position": int(state.get("queue_position") or 0),
        "queue_timeout_seconds": float(state.get("queue_timeout_seconds") or 0),
    }


def _timing_trace(response: dict[str, Any]) -> tuple[dict[str, int], dict[str, Any]]:
    timings = {
        str(key): max(0, int(value))
        for key, value in _dict(response.get("timings")).items()
        if isinstance(value, (int, float))
    }
    stages = {
        key: value
        for key, value in timings.items()
        if key.endswith("_ms") and key != "total_ms"
    }
    if not stages:
        return timings, {"stage": "", "elapsed_ms": 0}
    stage, elapsed_ms = max(stages.items(), key=lambda item: item[1])
    return timings, {"stage": stage, "elapsed_ms": elapsed_ms}


def build_medical_query_trace(
    response: dict[str, Any] | None,
    *,
    request_id: str = "",
    admission: dict[str, Any] | None = None,
    models: dict[str, Any] | None = None,
    lifecycle_status: str = "completed",
    error: str = "",
) -> dict[str, Any]:
    """Aggregate one medical-filter request into a stable machine trace."""
    response = _dict(response)
    first = _first_patient_result(response)
    conditions = _canonical_conditions(response)
    sources = _source_decisions(conditions)
    audits = _explanation_audits(response)
    overall = _dict(response.get("overall_result")) or _dict(first.get("overall_result"))

    issue_counts: Counter[tuple[str, str]] = Counter()

    def add_issue(layer: str, code: Any, count: int = 1) -> None:
        normalized = str(code or "").strip().upper()
        if normalized and count > 0:
            issue_counts[(layer, normalized)] += int(count)

    error_code = str(response.get("error_code") or "").strip()
    if error_code:
        add_issue("understanding", error_code)

    ir_quality = _dict(response.get(_IR_QUALITY_KEY)) or _dict(response.get("ir_quality"))
    for item in _list(ir_quality.get("issues")):
        add_issue("ir", _dict(item).get("code") or "IR_QUALITY_ISSUE")
    for item in _list(ir_quality.get("warnings")):
        add_issue("ir", _dict(item).get("code") or "IR_QUALITY_WARNING")

    evidence_plan = _dict(response.get(_EVIDENCE_PLAN_KEY)) or _dict(response.get("evidence_plan"))
    unresolved_count = int(evidence_plan.get("unresolved_count") or 0)
    if not unresolved_count:
        for item in _list(evidence_plan.get("conditions")):
            for source in _list(_dict(item).get("sources")):
                if str(_dict(source).get("resolution_status") or "resolved").lower() != "resolved":
                    unresolved_count += 1
    if unresolved_count:
        add_issue("routing", "UNRESOLVED_EVIDENCE_SOURCE", unresolved_count)

    for source in sources:
        status = str(source.get("status") or "UNKNOWN").upper()
        quality = str(source.get("data_quality") or "MISSING").upper()
        reason_code = str(source.get("reason_code") or "").upper()
        missing_capabilities = _list(source.get("missing_capabilities"))
        selection_complete = source.get("selection_complete") is not False

        if reason_code == "SOURCE_UNAVAILABLE" or quality == "SOURCE_ERROR":
            add_issue("evidence", reason_code or "SOURCE_UNAVAILABLE")
        elif quality in {"PARTIAL", "MISSING"} or status == "UNKNOWN":
            add_issue("evidence", reason_code or "DEGRADED_SOURCE")
        if missing_capabilities:
            add_issue("evidence", "MISSING_REQUIRED_CAPABILITY", len(missing_capabilities))
        if not selection_complete:
            add_issue("evidence", "INCOMPLETE_CANDIDATE_SET")
        if reason_code in _TEMPORAL_ISSUE_CODES:
            add_issue("temporal", reason_code)

    for condition in conditions:
        status = str(condition.get("status") or "UNKNOWN").upper()
        reason_code = str(condition.get("reason_code") or "").upper()
        conflict_level = str(condition.get("conflict_level") or "NONE").upper()
        if conflict_level != "NONE":
            add_issue("condition_adjudication", conflict_level)
        if status == "UNKNOWN":
            layer = "temporal" if reason_code in _TEMPORAL_ISSUE_CODES else "condition_adjudication"
            add_issue(layer, reason_code or "CONDITION_UNKNOWN")

    overall_status = str(
        overall.get("status")
        or response.get("status")
        or first.get("status")
        or "UNKNOWN"
    ).upper()
    overall_reason_code = str(overall.get("reason_code") or error_code or "").upper()
    if overall_status == "UNKNOWN":
        add_issue("overall_adjudication", overall_reason_code or "OVERALL_UNKNOWN")

    explanation_reason_codes: Counter[str] = Counter()
    for audit in audits:
        codes = [str(code).strip().upper() for code in _list(audit.get("reason_codes")) if str(code).strip()]
        explanation_reason_codes.update(codes)
        if audit.get("used_fallback") or audit.get("accepted") is False:
            if codes:
                for code in codes:
                    add_issue("explanation", code)
            else:
                add_issue("explanation", "EXPLANATION_FALLBACK")

    if lifecycle_status not in {"completed", "cancelled"} or error:
        add_issue("overall_adjudication", "REQUEST_FAILED")

    condition_status_counts = Counter(
        str(item.get("status") or "UNKNOWN").upper() for item in conditions
    )
    source_status_counts = Counter(
        str(item.get("status") or "UNKNOWN").upper() for item in sources
    )
    source_quality_counts = Counter(
        str(item.get("data_quality") or "MISSING").upper() for item in sources
    )
    source_uncertainty_kind_counts = Counter(
        _source_uncertainty_kind(item) for item in sources
    )
    unknown_source_uncertainty_kind_counts = Counter(
        _source_uncertainty_kind(item)
        for item in sources
        if str(item.get("status") or "UNKNOWN").upper() == "UNKNOWN"
    )
    conflict_count = sum(
        str(item.get("conflict_level") or "NONE").upper() != "NONE"
        for item in conditions
    )
    unavailable_count = sum(
        str(item.get("reason_code") or "").upper() == "SOURCE_UNAVAILABLE"
        or str(item.get("data_quality") or "").upper() == "SOURCE_ERROR"
        for item in sources
    )
    degraded_count = sum(
        str(item.get("data_quality") or "").upper() in {"PARTIAL", "MISSING", "SOURCE_ERROR"}
        or item.get("selection_complete") is False
        or bool(_list(item.get("missing_capabilities")))
        for item in sources
    )

    condition_qualities = [str(item.get("data_quality") or "") for item in conditions]
    condition_conflicts = [str(item.get("conflict_level") or "") for item in conditions]
    timings, bottleneck = _timing_trace(response)

    issues = [
        {"layer": layer, "code": code, "count": count}
        for (layer, code), count in sorted(
            issue_counts.items(),
            key=lambda item: (_ISSUE_LAYER_ORDER.get(item[0][0], 999), item[0][1]),
        )
    ]

    return {
        "schema_version": TRACE_SCHEMA_VERSION,
        "request_id": str(request_id or response.get("request_id") or ""),
        "lifecycle_status": str(lifecycle_status or "completed"),
        "outcome": {
            "status": overall_status,
            "reason_code": overall_reason_code,
            "data_quality": _worst_value(condition_qualities, _QUALITY_PRIORITY, "MISSING"),
            "conflict_level": _worst_value(condition_conflicts, _CONFLICT_PRIORITY, "NONE"),
        },
        "models": {
            str(key): str(value or "")
            for key, value in _dict(models).items()
            if str(key).strip()
        },
        "queue": _queue_trace(admission),
        "timings": timings,
        "bottleneck": bottleneck,
        "conditions": {
            "total": len(conditions),
            "status_counts": dict(sorted(condition_status_counts.items())),
            "conflict_count": conflict_count,
        },
        "sources": {
            "total": len(sources),
            "unavailable_count": unavailable_count,
            "degraded_count": degraded_count,
            "status_counts": dict(sorted(source_status_counts.items())),
            "quality_counts": dict(sorted(source_quality_counts.items())),
            "uncertainty_kind_counts": dict(sorted(source_uncertainty_kind_counts.items())),
            "unknown_uncertainty_kind_counts": dict(
                sorted(unknown_source_uncertainty_kind_counts.items())
            ),
        },
        "explanations": {
            "total": len(audits),
            "accepted_count": sum(audit.get("accepted") is True for audit in audits),
            "fallback_count": sum(
                audit.get("used_fallback") is True or audit.get("accepted") is False
                for audit in audits
            ),
            "reason_code_counts": dict(sorted(explanation_reason_codes.items())),
        },
        "issues": issues,
        "first_issue": dict(issues[0]) if issues else {},
        "error": str(error or "")[:300],
    }


def medical_query_trace_log(trace: dict[str, Any]) -> dict[str, Any]:
    """Return the concise subset intended for one-line application logs."""
    trace = _dict(trace)
    return {
        "request_id": trace.get("request_id", ""),
        "lifecycle_status": trace.get("lifecycle_status", ""),
        "outcome": _dict(trace.get("outcome")).get("status", "UNKNOWN"),
        "total_ms": _dict(trace.get("timings")).get("total_ms", 0),
        "queue_wait_ms": _dict(trace.get("queue")).get("wait_ms", 0),
        "bottleneck": trace.get("bottleneck", {}),
        "condition_count": _dict(trace.get("conditions")).get("total", 0),
        "source_count": _dict(trace.get("sources")).get("total", 0),
        "source_uncertainty": _dict(trace.get("sources")).get(
            "unknown_uncertainty_kind_counts",
            {},
        ),
        "first_issue": trace.get("first_issue", {}),
    }
