"""Deterministic quantifier adjudication for multi-record evidence."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Sequence

from microharness.medical.evidence import EvidenceStatus


_SUPPORTED_MODES = {
    "any", "all", "at_least", "more_than", "exact", "at_most",
    "less_than", "latest", "earliest",
}
_MODE_ALIASES = {
    "exists": "any", "first": "earliest", "last": "latest",
    "minimum": "at_least", "maximum": "at_most",
}


@dataclass(frozen=True)
class QuantifierDecision:
    applicable: bool
    status: EvidenceStatus = EvidenceStatus.UNKNOWN
    reason_code: str = ""
    reason: str = ""
    mode: str = ""
    count: float | None = None
    unit: str = ""
    selected_records: tuple[dict[str, Any], ...] = ()
    record_status_counts: Mapping[str, int] = field(default_factory=dict)
    selection_complete: bool = True


@dataclass(frozen=True)
class _RecordFact:
    record: dict[str, Any]
    status: EvidenceStatus
    scope_status: str
    event_time: datetime | None


def _quantifier_values(quantifier: object) -> tuple[str, float | None, str]:
    if isinstance(quantifier, Mapping):
        mode = str(quantifier.get("mode") or "").strip().lower()
        count_value = quantifier.get("count")
        unit = str(quantifier.get("unit") or "").strip()
    else:
        mode = str(getattr(quantifier, "mode", "") or "").strip().lower()
        count_value = getattr(quantifier, "count", None)
        unit = str(getattr(quantifier, "unit", "") or "").strip()
    mode = _MODE_ALIASES.get(mode, mode)
    try:
        count = float(count_value) if count_value is not None else None
    except (TypeError, ValueError):
        count = None
    return mode, count, unit


def _status(value: object) -> EvidenceStatus:
    text = str(value or "").strip().upper()
    if text in EvidenceStatus._value2member_map_:
        return EvidenceStatus(text)
    return EvidenceStatus.UNKNOWN


def _scope_status(value: object) -> str:
    text = str(value or "").strip().upper()
    if text in {"IN_SCOPE", "OUT_OF_SCOPE", "UNKNOWN", "NOT_REQUIRED"}:
        return text
    return "NOT_REQUIRED"


def _event_time(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _facts(records: Sequence[Mapping[str, Any]]) -> tuple[_RecordFact, ...]:
    return tuple(
        _RecordFact(
            record=dict(record),
            status=_status(record.get("record_status")),
            scope_status=_scope_status(record.get("scope_status")),
            event_time=_event_time(record.get("event_time")),
        )
        for record in records
        if isinstance(record, Mapping)
    )


def _display_count(value: float | None) -> str:
    if value is None:
        return ""
    return str(int(value)) if value.is_integer() else str(value)


def _counts(
    facts: Sequence[_RecordFact],
    *,
    unmaterialized_count: int = 0,
) -> dict[str, int]:
    result = {
        "total": len(facts), "in_scope": 0, "out_of_scope": 0,
        "matched": 0, "not_matched": 0, "unknown": 0,
        "unmaterialized": max(int(unmaterialized_count), 0),
    }
    for fact in facts:
        if fact.scope_status == "OUT_OF_SCOPE":
            result["out_of_scope"] += 1
            continue
        if fact.scope_status == "UNKNOWN":
            result["unknown"] += 1
            continue
        result["in_scope"] += 1
        if fact.status == EvidenceStatus.MATCHED:
            result["matched"] += 1
        elif fact.status == EvidenceStatus.NOT_MATCHED:
            result["not_matched"] += 1
        else:
            result["unknown"] += 1
    return result


def _decision(
    status: EvidenceStatus,
    reason_code: str,
    reason: str,
    *,
    mode: str,
    count: float | None,
    unit: str,
    selected: Sequence[_RecordFact],
    counts: Mapping[str, int],
    complete: bool,
) -> QuantifierDecision:
    return QuantifierDecision(
        applicable=True,
        status=status,
        reason_code=reason_code,
        reason=reason,
        mode=mode,
        count=count,
        unit=unit,
        selected_records=tuple(dict(item.record) for item in selected),
        record_status_counts=dict(counts),
        selection_complete=complete,
    )


def adjudicate_record_quantifier(
    candidate_records: Sequence[Mapping[str, Any]],
    quantifier: object,
    *,
    fallback_status: EvidenceStatus,
    candidate_count: int | None = None,
    records_complete: bool = True,
) -> QuantifierDecision:
    """Apply an explicit IR quantifier to canonical per-record decisions."""
    mode, count, unit = _quantifier_values(quantifier)
    if not mode:
        return QuantifierDecision(applicable=False)
    if mode not in _SUPPORTED_MODES:
        return QuantifierDecision(
            applicable=True,
            status=EvidenceStatus.UNKNOWN,
            reason_code="QUANTIFIER_UNSUPPORTED",
            reason=f"IR量词模式'{mode}'尚无确定性执行规则，当前无法判断",
            mode=mode,
            count=count,
            unit=unit,
            selection_complete=False,
        )

    facts = _facts(candidate_records)
    declared_gap = (
        max(int(candidate_count) - len(facts), 0)
        if candidate_count is not None
        else 0
    )
    unmaterialized_count = declared_gap
    if not records_complete and unmaterialized_count == 0:
        unmaterialized_count = 1
    materialized_complete = records_complete and unmaterialized_count == 0
    counts = _counts(facts, unmaterialized_count=unmaterialized_count)
    in_scope = [fact for fact in facts if fact.scope_status in {"IN_SCOPE", "NOT_REQUIRED"}]
    matched = [fact for fact in in_scope if fact.status == EvidenceStatus.MATCHED]
    failed = [fact for fact in in_scope if fact.status == EvidenceStatus.NOT_MATCHED]
    unknown = [
        fact for fact in facts
        if fact.scope_status == "UNKNOWN"
        or (fact.scope_status in {"IN_SCOPE", "NOT_REQUIRED"} and fact.status == EvidenceStatus.UNKNOWN)
    ]

    if not facts:
        status = (
            EvidenceStatus.UNKNOWN
            if fallback_status == EvidenceStatus.UNKNOWN or not materialized_complete
            else EvidenceStatus.NOT_MENTIONED
        )
        code = "QUANTIFIER_SOURCE_UNKNOWN" if status == EvidenceStatus.UNKNOWN else "NO_MATCHING_RECORD"
        reason = "候选记录不可用，无法执行量词判断" if status == EvidenceStatus.UNKNOWN else "未找到可用于量词判断的候选记录"
        return _decision(status, code, reason, mode=mode, count=count, unit=unit, selected=(), counts=counts, complete=materialized_complete)

    if mode == "any":
        if matched:
            return _decision(EvidenceStatus.MATCHED, "QUANTIFIER_ANY_MATCHED", f"任一记录条件成立：{len(matched)}条记录符合", mode=mode, count=count, unit=unit, selected=matched, counts=counts, complete=materialized_complete)
        if unknown or not materialized_complete:
            return _decision(EvidenceStatus.UNKNOWN, "QUANTIFIER_ANY_INDETERMINATE", "尚有记录缺少完整状态、时间或明细，无法确认是否存在符合记录", mode=mode, count=count, unit=unit, selected=unknown, counts=counts, complete=False)
        return _decision(EvidenceStatus.NOT_MATCHED, "QUANTIFIER_ANY_NOT_MET", "候选实体已出现，但没有记录满足全部约束", mode=mode, count=count, unit=unit, selected=failed, counts=counts, complete=materialized_complete)

    if mode == "all":
        if failed:
            return _decision(EvidenceStatus.NOT_MATCHED, "QUANTIFIER_ALL_NOT_MET", f"要求全部记录符合，但有{len(failed)}条记录不符合", mode=mode, count=count, unit=unit, selected=failed, counts=counts, complete=materialized_complete)
        if unknown or not materialized_complete:
            return _decision(EvidenceStatus.UNKNOWN, "QUANTIFIER_ALL_INDETERMINATE", "部分记录缺少完整状态、时间或明细，无法确认全部记录均符合", mode=mode, count=count, unit=unit, selected=unknown, counts=counts, complete=False)
        if matched:
            return _decision(EvidenceStatus.MATCHED, "QUANTIFIER_ALL_MATCHED", f"全部{len(matched)}条范围内记录均符合条件", mode=mode, count=count, unit=unit, selected=matched, counts=counts, complete=materialized_complete)
        return _decision(EvidenceStatus.NOT_MATCHED, "QUANTIFIER_ALL_EMPTY_SCOPE", "候选实体已出现，但目标范围内没有可满足条件的记录", mode=mode, count=count, unit=unit, selected=(), counts=counts, complete=materialized_complete)

    if mode in {"latest", "earliest"}:
        if unknown or not materialized_complete:
            return _decision(EvidenceStatus.UNKNOWN, "QUANTIFIER_SELECTION_INDETERMINATE", "记录时间或候选明细不完整，无法确定最早/最新记录", mode=mode, count=count, unit=unit, selected=unknown, counts=counts, complete=False)
        if not in_scope:
            return _decision(EvidenceStatus.NOT_MATCHED, "QUANTIFIER_SELECTION_EMPTY_SCOPE", "候选实体已出现，但目标范围内没有可选择的记录", mode=mode, count=count, unit=unit, selected=(), counts=counts, complete=materialized_complete)
        if any(fact.event_time is None for fact in in_scope):
            missing_time = [fact for fact in in_scope if fact.event_time is None]
            return _decision(EvidenceStatus.UNKNOWN, "QUANTIFIER_RECORD_TIME_MISSING", "存在缺少事件时间的候选记录，无法确定最早/最新记录", mode=mode, count=count, unit=unit, selected=missing_time, counts=counts, complete=False)
        selected = min(in_scope, key=lambda fact: fact.event_time) if mode == "earliest" else max(in_scope, key=lambda fact: fact.event_time)
        status = selected.status
        code = {EvidenceStatus.MATCHED: "QUANTIFIER_SELECTED_RECORD_MATCHED", EvidenceStatus.NOT_MATCHED: "QUANTIFIER_SELECTED_RECORD_NOT_MET", EvidenceStatus.UNKNOWN: "QUANTIFIER_SELECTED_RECORD_UNKNOWN"}[status]
        label = "最早" if mode == "earliest" else "最新"
        conclusion = "符合" if status == EvidenceStatus.MATCHED else "不符合" if status == EvidenceStatus.NOT_MATCHED else "无法完成"
        return _decision(status, code, f"按事件时间选择{label}记录后，该记录{conclusion}条件判断", mode=mode, count=count, unit=unit, selected=(selected,), counts=counts, complete=materialized_complete)

    if count is None or count < 0:
        return _decision(EvidenceStatus.UNKNOWN, "QUANTIFIER_COUNT_MISSING", "次数量词缺少有效数量，无法执行确定性统计", mode=mode, count=count, unit=unit, selected=(), counts=counts, complete=False)

    matched_count = len(matched)
    possible_count = matched_count + len(unknown)
    possible_count += counts["unmaterialized"]
    target = _display_count(count)
    if mode == "at_least":
        decided, impossible = matched_count >= count, possible_count < count
    elif mode == "more_than":
        decided, impossible = matched_count > count, possible_count <= count
    elif mode == "at_most":
        decided, impossible = possible_count <= count, matched_count > count
    elif mode == "less_than":
        decided, impossible = possible_count < count, matched_count >= count
    else:
        decided = matched_count == count and not unknown and materialized_complete
        impossible = matched_count > count or (possible_count < count and materialized_complete)

    if decided:
        return _decision(EvidenceStatus.MATCHED, "QUANTIFIER_COUNT_MATCHED", f"符合记录数为{matched_count}，满足量词{mode} {target}", mode=mode, count=count, unit=unit, selected=matched, counts=counts, complete=materialized_complete)
    if impossible:
        return _decision(EvidenceStatus.NOT_MATCHED, "QUANTIFIER_COUNT_NOT_MET", f"符合记录数为{matched_count}，不满足量词{mode} {target}", mode=mode, count=count, unit=unit, selected=matched, counts=counts, complete=materialized_complete)
    return _decision(EvidenceStatus.UNKNOWN, "QUANTIFIER_COUNT_INDETERMINATE", f"当前确认{matched_count}条符合，但仍有记录状态或明细不完整，无法判断是否满足量词{mode} {target}", mode=mode, count=count, unit=unit, selected=matched + unknown, counts=counts, complete=False)
