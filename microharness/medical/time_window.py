"""
Unified time-window resolution for medical filtering.

LLMs may label the user's temporal intent, but this module owns the deterministic
time parsing and comparison. It contains grammar and service-schema mappings, not
drug, diagnosis, or lab-item specific answers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from datetime import datetime, time, timedelta
from typing import Any, Optional


@dataclass
class TimeWindow:
    scope: str
    start: Optional[datetime] = None
    end: Optional[datetime] = None
    source: str = ""
    required: bool = False
    reason: str = ""

    @property
    def resolved(self) -> bool:
        return self.start is not None or self.end is not None

    def contains(self, value: datetime) -> bool:
        if self.start and value < self.start:
            return False
        if self.end and value > self.end:
            return False
        return True

    def describe(self) -> str:
        start = self.start.strftime("%Y-%m-%d %H:%M:%S") if self.start else "未知"
        end = self.end.strftime("%Y-%m-%d %H:%M:%S") if self.end else "未出院/开放"
        return f"{start} 至 {end}"

    def to_dict(self) -> dict:
        data = asdict(self)
        data["start"] = self.start.strftime("%Y-%m-%d %H:%M:%S") if self.start else ""
        data["end"] = self.end.strftime("%Y-%m-%d %H:%M:%S") if self.end else ""
        return data


_CANONICAL_EVENT_ALIASES = {
    "surgery": ("手术", "术"),
    "admission": ("入院",),
    "discharge": ("出院",),
    "encounter": ("住院", "就诊"),
}


def _temporal_value(temporal: object, name: str, default: Any = "") -> Any:
    if temporal is None:
        return default
    if isinstance(temporal, dict):
        return temporal.get(name, default)
    return getattr(temporal, name, default)


def _canonical_event_name(event: object) -> str:
    value = str(event or "").strip()
    lower = value.lower()
    aliases = {
        "operation": "surgery",
        "手术": "surgery",
        "术": "surgery",
        "入院": "admission",
        "出院": "discharge",
        "住院": "encounter",
        "就诊": "encounter",
    }
    return aliases.get(lower, aliases.get(value, lower))


def _event_aliases(event: object) -> list[str]:
    canonical = _canonical_event_name(event)
    if canonical in _CANONICAL_EVENT_ALIASES:
        return list(_CANONICAL_EVENT_ALIASES[canonical])
    value = str(event or "").strip()
    return [value] if value else []


def _normalize_datetime_text(text: str) -> str:
    text = str(text or "").strip()
    text = re.sub(r"(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日?", lambda m: f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}", text)
    text = re.sub(r"(\d{4})[/.](\d{1,2})[/.](\d{1,2})", lambda m: f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}", text)
    return text


def parse_datetime_value(date_value: str = "", time_value: str = "") -> Optional[datetime]:
    text = " ".join(str(x or "").strip() for x in (date_value, time_value) if str(x or "").strip())
    if not text:
        return None
    text = _normalize_datetime_text(text)
    m = re.search(r"(\d{4}-\d{2}-\d{2})(?:[ T]+(\d{2}:\d{2}(?::\d{2})?))?", text)
    if not m:
        return None
    time_part = m.group(2) or "00:00:00"
    if len(time_part) == 5:
        time_part += ":00"
    try:
        return datetime.strptime(f"{m.group(1)} {time_part}", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def parse_datetime_values(text: str) -> list[datetime]:
    """Extract one or more datetimes, including Chinese date ranges.

    Examples:
    - 2026年06月10日 15:29--2026年06月10日 16:15
    - 2026-06-10 15:29-16:15
    - 2026-06-10 15:29:00
    """
    text = _normalize_datetime_text(text)
    if not text:
        return []
    values: list[datetime] = []
    dt_fragment = r"\d{4}-\d{1,2}-\d{1,2}(?:[ T]+\d{1,2}:\d{2}(?::\d{2})?)?"
    start_end = re.search(
        rf"开始时间[:：]\s*({dt_fragment})\s*结束时间[:：]\s*({dt_fragment})",
        text,
    )
    if start_end:
        start = parse_datetime_value(start_end.group(1))
        end = parse_datetime_value(start_end.group(2))
        if start and end:
            if end < start:
                end += timedelta(days=1)
            return [start, end]
    full_pat = r"(\d{4}-\d{2}-\d{2})(?:[ T]+(\d{1,2}:\d{2}(?::\d{2})?))?"
    for m in re.finditer(full_pat, text):
        time_part = m.group(2) or "00:00:00"
        if len(time_part) == 5:
            time_part += ":00"
        try:
            values.append(datetime.strptime(f"{m.group(1)} {time_part}", "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            pass

    # Handle compact ranges where the end omits the date: 2026-06-10 15:29--16:15.
    if len(values) == 1:
        tail = text[text.find(values[0].strftime("%H:%M")[-5:]) + 5:]
        m = re.search(r"(?:--|~|至|-|—|－)\s*(\d{1,2}:\d{2}(?::\d{2})?)", tail)
        if m:
            end_time = m.group(1)
            if len(end_time) == 5:
                end_time += ":00"
            try:
                end = datetime.strptime(f"{values[0].strftime('%Y-%m-%d')} {end_time}", "%Y-%m-%d %H:%M:%S")
                if end < values[0]:
                    end += timedelta(days=1)
                values.append(end)
            except ValueError:
                pass
    return values


def detect_period_scope(text: str) -> str:
    text = text or ""
    if re.search(r"(住院期间|住院期内|本次住院|住院内|住院过程中)", text):
        return "住院期间"
    if _condition_mentions_encounter_anchor(text) or condition_needs_event_anchor(text):
        return "锚点相对时间"
    if re.search(r"(治疗期间|用药期间|检查期间|就诊期间)", text):
        return "事件期间"
    return ""


def requires_period_window(text: str) -> bool:
    return bool(detect_period_scope(text))


def _anchor_specs() -> list[dict[str, Any]]:
    """Read event-time anchors from the medical catalog metadata.

    A section becomes an event anchor when it declares:
    - anchor_field: true
    - anchor_aliases: ["手术", "术", ...]

    The date math below is generic; adding new events should be done by metadata,
    not by adding more event-specific functions.
    """
    specs: list[dict[str, Any]] = []
    try:
        from microharness.medical.query_router import DOCUMENT_CATALOG
        for doc_name, doc_info in (DOCUMENT_CATALOG or {}).items():
            for section in doc_info.get("sections", []) or []:
                if not isinstance(section, dict) or not section.get("anchor_field"):
                    continue
                aliases = section.get("anchor_aliases") or []
                if isinstance(aliases, str):
                    aliases = [aliases]
                aliases = [str(a).strip() for a in aliases if str(a).strip()]
                if not aliases:
                    continue
                specs.append({
                    "doc": doc_name,
                    "label": section.get("name", ""),
                    "aliases": aliases,
                    "event": str(
                        section.get("anchor_event")
                        or section.get("event")
                        or ""
                    ).strip(),
                    "source": f"{doc_name}.{section.get('name', '')}",
                    "time_role": section.get("time_role", "range"),
                })
    except Exception:
        pass
    return specs


def _alias_pattern(aliases: list[str]) -> str:
    return "|".join(re.escape(a) for a in sorted(aliases or [], key=len, reverse=True))


def _condition_mentions_anchor(condition: str, aliases: list[str]) -> bool:
    text = condition or ""
    alias_pat = _alias_pattern(aliases)
    if not alias_pat:
        return False
    amount = r"(?:\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百千万亿]+)"
    unit = r"(?:分钟|小时|天|日|周|月|个月)"
    return bool(re.search(
        rf"(?:{alias_pat})\s*(?:前|后|中|期间|当天|当日|时|{amount}\s*{unit}\s*(?:前|后|内)|(?:前|后)?\s*第\s*{amount}\s*{unit})",
        text,
    ))


def _condition_mentions_encounter_anchor(condition: str) -> bool:
    amount = r"(?:\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百千万亿]+)"
    unit = r"(?:分钟|小时|天|日|周|月|个月)"
    return bool(re.search(
        rf"(入院|出院)\s*(?:前|后|时|当天|当日|{amount}\s*{unit}\s*(?:前|后|内)|(?:前|后)?\s*第\s*{amount}\s*{unit})",
        condition or "",
    ))


def _spec_matches_event(spec: dict[str, Any], event: object) -> bool:
    canonical_event = _canonical_event_name(event)
    declared_event = _canonical_event_name(spec.get("event"))
    if declared_event and declared_event == canonical_event:
        return True
    expected_aliases = set(_event_aliases(canonical_event))
    return bool(expected_aliases & set(spec.get("aliases") or []))


def get_anchor_route_for_condition(
    condition: str = "",
    *,
    temporal: object = None,
    allow_text_fallback: bool = True,
) -> tuple[list[str], list[str]]:
    """Return document/section route required to resolve event-relative time."""
    docs: list[str] = []
    sections: list[str] = []
    for spec in _anchor_specs():
        structured_match = bool(temporal) and _spec_matches_event(
            spec,
            _temporal_value(temporal, "event"),
        )
        text_match = allow_text_fallback and _condition_mentions_anchor(
            condition,
            spec.get("aliases", []),
        )
        if not structured_match and not text_match:
            continue
        if spec["doc"] not in docs:
            docs.append(spec["doc"])
        if spec["label"] not in sections:
            sections.append(spec["label"])
    return docs, sections


def condition_needs_event_anchor(
    condition: str,
    *,
    temporal: object = None,
    allow_text_fallback: bool = True,
) -> bool:
    docs, sections = get_anchor_route_for_condition(
        condition,
        temporal=temporal,
        allow_text_fallback=allow_text_fallback,
    )
    return bool(docs and sections)


def _iter_bindings(service_results: list[dict[str, Any]]):
    for item in service_results or []:
        if not isinstance(item, dict) or item.get("service_error"):
            continue
        for binding in item.get("bindings", []) or []:
            if isinstance(binding, dict):
                yield binding


def _clean_label(label: str) -> str:
    label = str(label or "")
    if label.startswith("[") and "] " in label:
        return label.split("] ", 1)[1]
    return label


def _extract_encounter_bounds(service_results: list[dict[str, Any]]) -> tuple[Optional[datetime], Optional[datetime]]:
    start_date = start_time = end_date = end_time = ""
    for binding in _iter_bindings(service_results):
        label = _clean_label(str(binding.get("html_field") or ""))
        eng = str(binding.get("eng_field") or binding.get("xml_path", "").split("/")[-1] or "")
        value = str(binding.get("html_value") or binding.get("value") or "").strip()
        if not value:
            continue
        if eng in {"encStartDate", "admissionDate"} or label in {"入院日期", "入院日期时间", "入院时间"}:
            start_date = value
        elif eng in {"encStartTime", "admissionTime"}:
            start_time = value
        elif eng in {"encEndDate", "dischargeDate"} or label in {"出院日期", "出院日期时间", "出院时间"}:
            end_date = value
        elif eng in {"encEndTime", "dischargeTime"}:
            end_time = value

    start = parse_datetime_value(start_date, start_time)
    end = parse_datetime_value(end_date, end_time) if end_date or end_time else None
    return start, end


def extract_encounter_window(service_results: list[dict[str, Any]]) -> TimeWindow:
    start, end = _extract_encounter_bounds(service_results)
    if not start:
        return TimeWindow(scope="住院期间", source="encounter-info", required=True, reason="缺少入院时间")
    return TimeWindow(scope="住院期间", start=start, end=end, source="encounter-info", required=True)


def extract_event_anchor_window(
    condition: str,
    records: list[dict[str, Any]],
    preferred_aliases: list[str] | None = None,
    *,
    preferred_event: object = "",
    selection: str = "unspecified",
) -> TimeWindow:
    specs = _anchor_specs()
    if preferred_event:
        specs = [s for s in specs if _spec_matches_event(s, preferred_event)]
    elif preferred_aliases:
        specs = [s for s in specs if set(s.get("aliases", [])) & set(preferred_aliases)]
    elif condition:
        specs = [s for s in specs if _condition_mentions_anchor(condition, s.get("aliases", []))]

    candidates: list[TimeWindow] = []
    for spec in specs:
        for item in records or []:
            if not isinstance(item, dict) or item.get("service_error"):
                continue
            file_name = str(item.get("file", ""))
            if spec.get("doc") and spec["doc"] not in file_name:
                continue
            for binding in item.get("bindings", []) or []:
                if not isinstance(binding, dict):
                    continue
                label = _clean_label(str(binding.get("html_field") or ""))
                value = str(binding.get("html_value") or binding.get("value") or "").strip()
                if not value or label != spec.get("label"):
                    continue
                values = parse_datetime_values(value)
                if values:
                    start = values[0]
                    end = values[1] if len(values) > 1 else values[0]
                    candidates.append(TimeWindow(
                        scope="事件期间",
                        start=start,
                        end=end,
                        source=spec.get("source", ""),
                        required=True,
                    ))
    if candidates:
        normalized_selection = str(selection or "unspecified").strip().lower()
        if normalized_selection == "first":
            return min(candidates, key=lambda item: item.start or datetime.max)
        if normalized_selection == "last":
            return max(candidates, key=lambda item: item.start or datetime.min)
        return candidates[0]
    source = specs[0].get("source", "") if specs else ""
    reason = "缺少事件时间锚点" if specs else "未找到事件对应的时间锚点元数据"
    return TimeWindow(scope="事件期间", source=source, required=True, reason=reason)


def extract_encounter_anchor_window(
    condition: str,
    service_results: dict[str, list],
    *,
    event: object = "",
) -> TimeWindow:
    start, end = _extract_encounter_bounds((service_results or {}).get("encounter-info", []))
    text = condition or ""
    canonical_event = _canonical_event_name(event)
    if canonical_event == "admission" or (not canonical_event and "入院" in text):
        if not start:
            return TimeWindow(scope="入院时间锚点", source="encounter-info", required=True, reason="缺少入院时间")
        return TimeWindow(
            scope="入院时间锚点",
            start=start,
            end=start,
            source="encounter-info",
            required=True,
            reason="使用就诊信息的入院日期时间",
        )
    if canonical_event == "discharge" or (not canonical_event and "出院" in text):
        if not end:
            return TimeWindow(scope="出院时间锚点", source="encounter-info", required=True, reason="缺少出院时间，可能仍在住院")
        return TimeWindow(
            scope="出院时间锚点",
            start=end,
            end=end,
            source="encounter-info",
            required=True,
            reason="使用就诊信息的出院日期时间",
        )
    return TimeWindow(scope="就诊时间锚点", source="encounter-info", required=True, reason="未识别入院/出院锚点")


def _normalize_temporal_relation(relation: object) -> str:
    value = str(relation or "").strip().lower()
    aliases = {
        "前": "before",
        "之前": "before",
        "后": "after",
        "之后": "after",
        "期间": "during",
        "中": "during",
        "时": "during",
    }
    return aliases.get(value, value)


def _offset_window_from_temporal(temporal: object, event: TimeWindow) -> TimeWindow:
    if not event.start and not event.end:
        return TimeWindow(
            scope="锚点相对时间",
            source=event.source,
            required=True,
            reason=event.reason or "缺少事件时间锚点",
        )

    relation = _normalize_temporal_relation(_temporal_value(temporal, "relation"))
    duration = _temporal_value(temporal, "duration", None)
    unit = str(_temporal_value(temporal, "unit") or "").strip()
    event_start = event.start or event.end
    event_end = event.end or event.start

    if relation == "during":
        return TimeWindow(
            scope="事件期间",
            start=event_start,
            end=event_end,
            source=event.source,
            required=True,
        )
    if relation not in {"before", "after"}:
        return TimeWindow(
            scope="锚点相对时间",
            source=event.source,
            required=True,
            reason="结构化时间关系缺失或不支持",
        )

    if duration is None:
        if relation == "before":
            return TimeWindow(
                scope="事件前",
                start=None,
                end=event_start,
                source=event.source,
                required=True,
            )
        return TimeWindow(
            scope="事件后",
            start=event_end,
            end=None,
            source=event.source,
            required=True,
        )

    try:
        numeric_duration = float(duration)
    except (TypeError, ValueError):
        numeric_duration = -1
    if numeric_duration < 0:
        return TimeWindow(
            scope="锚点相对时间",
            source=event.source,
            required=True,
            reason="结构化时间时长无效",
        )
    if not unit:
        return TimeWindow(
            scope="锚点相对时间",
            source=event.source,
            required=True,
            reason="结构化时间单位缺失",
        )

    from microharness.medical.temporal_parser import normalize_time_unit, convert_numeric_unit

    normalized_unit = normalize_time_unit(unit)
    if normalized_unit not in {"分钟", "小时", "天", "周", "月"}:
        return TimeWindow(
            scope="锚点相对时间",
            source=event.source,
            required=True,
            reason=f"不支持的结构化时间单位：{unit}",
        )
    try:
        delta = timedelta(
            hours=convert_numeric_unit(numeric_duration, normalized_unit, "小时")
        )
    except (TypeError, ValueError):
        return TimeWindow(
            scope="锚点相对时间",
            source=event.source,
            required=True,
            reason=f"不支持的结构化时间单位：{unit}",
        )
    if relation == "before":
        return TimeWindow(
            scope="事件前时间窗",
            start=event_start - delta,
            end=event_start,
            source=event.source,
            required=True,
        )
    return TimeWindow(
        scope="事件后时间窗",
        start=event_end,
        end=event_end + delta,
        source=event.source,
        required=True,
    )


def _offset_window_from_event(condition: str, event: TimeWindow, aliases: list[str] | None = None) -> TimeWindow:
    text = condition or ""
    if not event.start and not event.end:
        return TimeWindow(scope="锚点相对时间", source=event.source, required=True, reason=event.reason or "缺少事件时间锚点")
    aliases = aliases or []
    alias_pat = _alias_pattern(aliases) if aliases else r".{0,8}"
    event_start = event.start or event.end
    event_end = event.end or event.start

    if re.search(rf"(?:{alias_pat})(?:前)\s*(当天|当日)", text):
        return TimeWindow(
            scope="事件前当天",
            start=datetime.combine(event_start.date(), time.min),
            end=event_start,
            source=event.source,
            required=True,
        )
    if re.search(rf"(?:{alias_pat})(?:后)\s*(当天|当日)", text):
        return TimeWindow(
            scope="事件后当天",
            start=event_end,
            end=datetime.combine(event_end.date(), time.max).replace(microsecond=0),
            source=event.source,
            required=True,
        )
    if re.search(rf"(?:{alias_pat})\s*(当天|当日)", text):
        return TimeWindow(
            scope="事件当天",
            start=datetime.combine(event_start.date(), time.min),
            end=datetime.combine(event_start.date(), time.max).replace(microsecond=0),
            source=event.source,
            required=True,
        )
    if re.search(rf"(?:{alias_pat})(?:期间|中|时)", text):
        return event

    m = re.search(rf"(?:{alias_pat})(前|后)\s*(\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百千万亿]+)\s*(分钟|小时|天|日|周|月|个月)?\s*(内)?", text)
    order = "direction_first"
    if not m:
        m = re.search(rf"(?:{alias_pat})\s*(\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百千万亿]+)\s*(分钟|小时|天|日|周|月|个月)\s*(前|后|内)", text)
        order = "amount_first" if m else order
    if not m:
        m = re.search(rf"(?:{alias_pat})(?:后)?\s*第\s*(\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百千万亿]+)\s*(天|日|小时|分钟|周|月|个月)", text)
        order = "ordinal_after" if m else order
    if not m:
        if re.search(rf"(?:{alias_pat})\s*前", text):
            return TimeWindow(scope="事件前", start=None, end=event_start, source=event.source, required=True)
        if re.search(rf"(?:{alias_pat})\s*后", text):
            return TimeWindow(scope="事件后", start=event_end, end=None, source=event.source, required=True)
        return TimeWindow(scope="锚点相对时间", source=event.source, required=True, reason="未识别事件前后时间偏移")
    from microharness.medical.temporal_parser import parse_cn_number, normalize_time_unit, convert_numeric_unit

    if order == "direction_first":
        direction = m.group(1)
        amount_text = m.group(2)
        unit_text = m.group(3) or "天"
    elif order == "amount_first":
        amount_text = m.group(1)
        unit_text = m.group(2)
        direction = "后" if m.group(3) == "内" else m.group(3)
    else:
        amount_text = m.group(1)
        unit_text = m.group(2)
        direction = "第"
    amount = parse_cn_number(amount_text)
    unit = normalize_time_unit(unit_text)
    if amount is None:
        return TimeWindow(scope="锚点相对时间", source=event.source, required=True, reason="未识别时间数量")
    delta = timedelta(hours=convert_numeric_unit(float(amount), unit, "小时"))
    if direction == "第":
        unit_hours = convert_numeric_unit(1.0, unit, "小时")
        start_offset = timedelta(hours=max(float(amount) - 1, 0) * unit_hours)
        end_offset = timedelta(hours=float(amount) * unit_hours)
        return TimeWindow(scope="事件后第N时间窗", start=event_end + start_offset, end=event_end + end_offset, source=event.source, required=True)
    if direction == "前":
        return TimeWindow(scope="事件前时间窗", start=event_start - delta, end=event_start, source=event.source, required=True)
    return TimeWindow(scope="事件后时间窗", start=event_end, end=event_end + delta, source=event.source, required=True)


def resolve_time_window(
    condition: str,
    service_results: dict[str, list],
    records: list[dict[str, Any]] | None = None,
    *,
    temporal: object = None,
    allow_text_fallback: bool = True,
) -> Optional[TimeWindow]:
    if temporal is not None:
        scope = str(_temporal_value(temporal, "scope") or "").strip().lower()
        event = _canonical_event_name(_temporal_value(temporal, "event"))
        relation = _normalize_temporal_relation(_temporal_value(temporal, "relation"))

        if scope == "encounter" or (event == "encounter" and relation == "during"):
            return extract_encounter_window(
                (service_results or {}).get("encounter-info", [])
            )

        if event in {"admission", "discharge"}:
            anchor = extract_encounter_anchor_window(
                "",
                service_results or {},
                event=event,
            )
            return _offset_window_from_temporal(temporal, anchor)

        if event:
            anchor_docs, _ = get_anchor_route_for_condition(
                temporal=temporal,
                allow_text_fallback=False,
            )
            if not anchor_docs:
                return TimeWindow(
                    scope="锚点相对时间",
                    required=True,
                    reason=f"未找到事件{event}对应的时间锚点元数据",
                )
            anchor = extract_event_anchor_window(
                "",
                records or [],
                preferred_event=event,
                selection=str(_temporal_value(temporal, "selection") or "unspecified"),
            )
            return _offset_window_from_temporal(temporal, anchor)

        raw_scope = str(_temporal_value(temporal, "scope") or "结构化时间约束")
        return TimeWindow(
            scope=raw_scope,
            required=True,
            reason="结构化时间约束缺少可解析的事件锚点",
        )

    if not allow_text_fallback:
        return None

    scope = detect_period_scope(condition)
    if not scope:
        return None
    if scope == "住院期间":
        return extract_encounter_window((service_results or {}).get("encounter-info", []))
    if _condition_mentions_encounter_anchor(condition):
        aliases = ["入院"] if "入院" in condition else ["出院"]
        event = extract_encounter_anchor_window(condition, service_results or {})
        return _offset_window_from_event(condition, event, aliases=aliases)
    anchor_docs, _ = get_anchor_route_for_condition(condition)
    if anchor_docs:
        specs = [s for s in _anchor_specs() if s.get("doc") in anchor_docs and _condition_mentions_anchor(condition, s.get("aliases", []))]
        event = extract_event_anchor_window(condition, records or [])
        aliases = specs[0].get("aliases", []) if specs else []
        return _offset_window_from_event(condition, event, aliases=aliases)
    return TimeWindow(scope=scope, required=True, reason=f"暂未解析{scope}的结构化时间锚点")
