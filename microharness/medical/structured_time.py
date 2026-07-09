"""
Generic time-window filtering for structured medical service records.

This module is intentionally schema-oriented: it looks for date/time fields in
service bindings and compares them with a resolved TimeWindow. It does not encode
drug, diagnosis, or lab-item specific answers.
"""

from __future__ import annotations

from typing import Any

from microharness.medical.time_window import TimeWindow, parse_datetime_value, parse_datetime_values


def _split_prefixed_label(label: str) -> tuple[str, str]:
    label = str(label or "")
    if label.startswith("[") and "] " in label:
        prefix, field = label.split("] ", 1)
        return prefix + "]", field
    return "", label


def _record_sort_key(prefix: str) -> tuple[str, int]:
    digits = "".join(ch for ch in prefix if ch.isdigit())
    return (prefix[: prefix.find(digits)] if digits else prefix, int(digits or 0))


def _is_datetime_label(label: str, eng: str) -> bool:
    lower = str(eng or "").lower()
    return (
        "日期时间" in label
        or "时间点" in label
        or lower.endswith("datetime")
        or lower.endswith("date_time")
        or lower in {"datetime", "recordtime", "eventtime", "ordertime"}
    )


def _is_date_label(label: str, eng: str) -> bool:
    lower = str(eng or "").lower()
    return "日期" in label or lower.endswith("date")


def _is_time_label(label: str, eng: str) -> bool:
    lower = str(eng or "").lower()
    return ("时间" in label and "日期时间" not in label) or lower.endswith("time")


def _first_record_time(fields: list[dict[str, str]]):
    date_value = ""
    time_value = ""

    for item in fields:
        label = item.get("label", "")
        eng = item.get("eng", "")
        value = item.get("value", "")
        if not value:
            continue
        if _is_datetime_label(label, eng):
            values = parse_datetime_values(value)
            if values:
                return values[0]
            parsed = parse_datetime_value(value)
            if parsed:
                return parsed

    for item in fields:
        label = item.get("label", "")
        eng = item.get("eng", "")
        value = item.get("value", "")
        if not value:
            continue
        if not date_value and _is_date_label(label, eng):
            date_value = value
        elif not time_value and _is_time_label(label, eng):
            time_value = value

    parsed = parse_datetime_value(date_value, time_value)
    if parsed:
        return parsed

    for item in fields:
        values = parse_datetime_values(item.get("value", ""))
        if values:
            return values[0]
    return None


def _format_record_line(prefix: str, fields: list[dict[str, str]]) -> str:
    parts = [f"{item['label']}: {item['value']}" for item in fields if item.get("value")]
    return f"  {prefix} " + " | ".join(parts) if prefix else "  " + " | ".join(parts)


def _format_time(value) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S") if value else "未取得"


def _field_value(fields: list[dict[str, str]], names: tuple[str, ...]) -> str:
    for item in fields:
        label = item.get("label", "")
        eng = item.get("eng", "")
        if any(name in label or name == eng for name in names):
            value = item.get("value", "")
            if value:
                return value
    return ""


def _candidate_detail(
    item: dict[str, Any],
    *,
    in_window: bool,
    time_window: TimeWindow,
) -> dict[str, Any]:
    fields = item.get("fields") or []
    detail: dict[str, Any] = {
        "记录": item["prefix"],
        "记录时间": _format_time(item["time"]),
        "是否在时间窗": in_window,
        "时间窗": time_window.describe(),
    }
    for field in fields:
        label = str(field.get("label") or "").strip()
        value = str(field.get("value") or "").strip()
        if label and value and label not in detail:
            detail[label] = value
    return detail


def _first_field_with_label(
    fields: list[dict[str, str]],
    include: tuple[str, ...],
    exclude: tuple[str, ...] = (),
) -> tuple[str, str]:
    for field in fields:
        label = str(field.get("label") or "")
        value = str(field.get("value") or "").strip()
        if not value:
            continue
        if any(token in label for token in include) and not any(token in label for token in exclude):
            return label, value
    return "", ""


def _first_display_name(fields: list[dict[str, str]]) -> tuple[str, str]:
    preferred = (("名称",), ("项目",), ("描述",), ("标题",), ("章节",))
    for must_have in preferred:
        for field in fields:
            label = str(field.get("label") or "")
            value = str(field.get("value") or "").strip()
            if value and all(token in label for token in must_have):
                return label, value
    return _first_field_with_label(fields, ("名称", "项目", "描述", "标题", "章节"), ("科室", "医生", "医师", "人员"))


def _first_display_time(fields: list[dict[str, str]]) -> tuple[str, str]:
    for field in fields:
        label = str(field.get("label") or "")
        eng = str(field.get("eng") or "")
        value = str(field.get("value") or "").strip()
        if value and "开立" in label and (_is_datetime_label(label, eng) or "日期" in label):
            return label, value
    for field in fields:
        label = str(field.get("label") or "")
        eng = str(field.get("eng") or "")
        value = str(field.get("value") or "").strip()
        if value and _is_datetime_label(label, eng):
            return label, value
    return "", ""


def _candidate_time_example(item: dict[str, Any], time_window: TimeWindow, *, in_window: bool) -> str:
    fields = item.get("fields") or []
    name_label, display_name = _first_display_name(fields)
    time_label, display_time = _first_display_time(fields)
    _, route = _first_field_with_label(fields, ("途径", "方式"))
    if display_name:
        parts = [f"{item['prefix']} {name_label or '项目'}={display_name}"]
        parts.append(f"{time_label or '记录时间'}={display_time or _format_time(item['time'])}")
        if route:
            parts.append(f"途径/方式={route}")
        judgement = "在" if in_window else "不在"
        parts.append(f"{judgement}{time_window.scope}（范围：{time_window.describe()}）")
        return "，".join(parts)
    return f"{item['prefix']} 记录时间={_format_time(item['time'])}"


def _semantic_time_match(
    condition: str,
    fields: list[dict[str, str]],
    time_window: TimeWindow,
    temporal_semantics: dict[str, Any] | None,
) -> tuple[bool, str]:
    """Use service metadata as semantic time evidence when present.

    Some service timestamps are record/entry timestamps. A service may declare a
    clinical temporal field in SKILL.md, for example a type/status field whose
    values carry admission/preoperative/discharge semantics.
    """
    cfg = temporal_semantics if isinstance(temporal_semantics, dict) else {}
    field_name = str(cfg.get("field") or "").strip()
    rules = cfg.get("rules") if isinstance(cfg.get("rules"), list) else []
    if not field_name or not rules:
        return False, ""

    text = f"{condition or ''} {time_window.scope or ''}"
    field_value = _field_value(fields, (field_name,))
    if not field_value:
        return False, ""
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        query_terms = [str(x) for x in (rule.get("query_terms") or []) if str(x)]
        values = [str(x) for x in (rule.get("values") or []) if str(x)]
        if query_terms and not any(term in text for term in query_terms):
            continue
        if values and any(value in field_value for value in values):
            return True, str(rule.get("reason") or f"{field_name}符合时间语义")
    return False, ""


def filter_bindings_by_time_window(
    bindings: list[dict[str, Any]],
    time_window: TimeWindow | None,
    condition: str = "",
    temporal_semantics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Filter structured service bindings by a resolved time window.

    Returns:
      applicable: whether a time filter was actually applied.
      matched: whether at least one record falls inside the time window.
      filtered_lines: compact text lines for records inside the window.
      reason: user-facing explanation for no match / missing time.
    """
    if not time_window or not time_window.required:
        return {"applicable": False}

    records: dict[str, list[dict[str, str]]] = {}
    for binding in bindings or []:
        raw_label = str(binding.get("html_field") or "")
        prefix, label = _split_prefixed_label(raw_label)
        value = str(binding.get("html_value") or binding.get("value") or "").strip()
        if not value:
            continue
        records.setdefault(prefix, []).append(
            {
                "label": label,
                "value": value,
                "eng": str(binding.get("eng_field") or binding.get("xml_path", "").split("/")[-1] or ""),
            }
        )

    if not records:
        return {"applicable": False}

    in_window: list[dict[str, Any]] = []
    semantic_window: list[dict[str, Any]] = []
    outside: list[dict[str, Any]] = []
    missing_time: list[dict[str, Any]] = []
    for prefix in sorted(records, key=_record_sort_key):
        fields = records[prefix]
        record_time = _first_record_time(fields)
        semantic_match, semantic_reason = _semantic_time_match(
            condition,
            fields,
            time_window,
            temporal_semantics,
        )
        item = {
            "prefix": prefix or "[记录]",
            "time": record_time,
            "line": _format_record_line(prefix, fields),
            "semantic_reason": semantic_reason,
            "fields": fields,
        }
        if semantic_match:
            semantic_window.append(item)
        elif not time_window.resolved:
            missing_time.append(item)
        elif not record_time:
            missing_time.append(item)
        elif time_window.contains(record_time):
            in_window.append(item)
        else:
            outside.append(item)

    if semantic_window:
        return {
            "applicable": True,
            "matched": True,
            "filtered_lines": [item["line"] for item in semantic_window],
            "reason": "；".join(dict.fromkeys(item["semantic_reason"] for item in semantic_window if item.get("semantic_reason"))),
            "candidate_records": [
                {
                    "记录": item["prefix"],
                    "记录时间": _format_time(item["time"]),
                    "是否在时间窗": True,
                    "时间判断": item.get("semantic_reason") or "诊断类型符合时间语义",
                }
                for item in semantic_window
            ],
        }

    if not time_window.resolved:
        return {"applicable": False}

    if in_window:
        return {
            "applicable": True,
            "matched": True,
            "filtered_lines": [item["line"] for item in in_window],
            "candidate_records": [
                _candidate_detail(item, in_window=True, time_window=time_window)
                for item in in_window
            ],
        }

    if outside:
        examples = "；".join(
            _candidate_time_example(item, time_window, in_window=False)
            for item in outside[:5]
        )
        more = f"；另有{len(outside) - 5}条" if len(outside) > 5 else ""
        reason = (
            f"找到{len(outside)}条候选记录，但记录时间不在{time_window.scope}"
            f"（范围：{time_window.describe()}）：{examples}{more}"
        )
        return {
            "applicable": True,
            "matched": False,
            "reason": reason,
            "fields": "\n".join(item["line"] for item in outside[:20]),
            "candidate_records": [
                _candidate_detail(item, in_window=False, time_window=time_window)
                for item in outside
            ],
        }

    if missing_time:
        reason = (
            f"找到{len(missing_time)}条候选记录，但缺少可比较的记录时间，"
            f"无法判断是否发生在{time_window.scope}"
        )
        return {
            "applicable": True,
            "matched": False,
            "reason": reason,
            "fields": "\n".join(item["line"] for item in missing_time[:20]),
            "candidate_records": [
                {"记录": item["prefix"], "记录时间": "未取得", "是否在时间窗": False}
                for item in missing_time
            ],
        }

    return {"applicable": False}
