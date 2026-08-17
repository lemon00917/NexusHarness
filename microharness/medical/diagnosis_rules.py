"""Deterministic certainty checks for structured diagnosis records."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from microharness.medical.entity_normalization import entity_candidates as normalized_entity_candidates
from microharness.medical.record_identity import display_record_reference, identity_from_binding
from microharness.medical.structured_time import filter_bindings_by_time_window
from microharness.medical.time_window import TimeWindow, parse_datetime_value


_UNCERTAIN_PATTERN = re.compile(
    r"疑似|可疑|待排|待除外|不除外|不能排除|尚不能排除|可能|考虑|倾向|拟诊|[?？]"
)
_EXCLUDED_PATTERN = re.compile(r"已排除|明确排除|排除诊断|否定诊断|未诊断|无明确诊断")
_REJECTED_STATUS_PATTERN = re.compile(r"无效|作废|删除|撤销|取消")


def _normalize(value: Any) -> str:
    return re.sub(r"[\s\u3000,，。；;：:、()（）\[\]【】]", "", str(value or "")).lower()


def _split_label(binding: dict[str, Any]) -> tuple[str, str]:
    label = str(binding.get("html_field") or "").strip()
    matched = re.match(r"^\[([^\]]+)\]\s*(.*)$", label)
    if matched:
        return matched.group(1).strip(), matched.group(2).strip()
    return "诊断1", label


def _field_key(binding: dict[str, Any]) -> str:
    eng = str(binding.get("eng_field") or "").strip()
    return eng or str(binding.get("xml_path") or "").rsplit("/", 1)[-1].strip()


def _field_value(binding: dict[str, Any]) -> str:
    return str(binding.get("html_value") or binding.get("value") or "").strip()


@dataclass
class DiagnosisRecord:
    prefix: str
    record_id: str = ""
    record_id_label: str = ""
    record_id_field: str = ""
    bindings: list[dict[str, Any]] = field(default_factory=list)
    fields: dict[str, str] = field(default_factory=dict)
    raw_fields: dict[str, str] = field(default_factory=dict)

    def _value(self, eng: str, label: str) -> str:
        if self.raw_fields.get(eng):
            return self.raw_fields[eng]
        return next((value for key, value in self.fields.items() if label in key), "")

    @property
    def name(self) -> str:
        return self._value("diagnoseName", "诊断名称")

    @property
    def diagnosis_type(self) -> str:
        return self._value("diagTypeDesc", "诊断类型")

    @property
    def status(self) -> str:
        return self._value("diagStatusDesc", "诊断状态")

    @property
    def remarks(self) -> str:
        return self._value("diagnoseRemarks", "诊断备注")

    @property
    def category(self) -> str:
        return self._value("diagCategory", "诊断分类")

    @property
    def diagnosis_date(self) -> str:
        return self._value("diagnoseDate", "诊断日期")

    @property
    def diagnosis_clock(self) -> str:
        return self._value("diagnoseTime", "诊断时间")

    @property
    def diagnosis_datetime(self) -> str:
        date_text = self.diagnosis_date.strip()
        time_text = self.diagnosis_clock.strip()
        if date_text and time_text:
            # Some sources put 00:00:00 in the date field and the real clock
            # value in a separate field. Prefer the explicitly supplied clock.
            date_part = re.split(r"[ T]", date_text, maxsplit=1)[0]
            parsed = parse_datetime_value(date_part, time_text)
            if parsed:
                return parsed.strftime("%Y-%m-%d %H:%M:%S")
            return f"{date_text} {time_text}".strip()
        if date_text:
            return date_text
        return time_text

    def compact_line(self) -> str:
        parts = [f"{key}={value}" for key, value in self.fields.items() if value]
        return f"[{self.prefix}] " + "，".join(parts)

    def display_reference(self) -> str:
        return display_record_reference(self.prefix, self.record_id, self.record_id_label)


def diagnosis_records_from_bindings(bindings: list[dict[str, Any]]) -> list[DiagnosisRecord]:
    grouped: dict[str, DiagnosisRecord] = {}
    for binding in bindings or []:
        if not isinstance(binding, dict):
            continue
        prefix, label = _split_label(binding)
        value = _field_value(binding)
        if not value:
            continue
        record = grouped.setdefault(prefix, DiagnosisRecord(prefix=prefix))
        identity = identity_from_binding(binding)
        if identity and not record.record_id:
            record.record_id = identity["record_id"]
            record.record_id_label = identity["record_id_label"]
            record.record_id_field = identity["record_id_field"]
        record.bindings.append(binding)
        record.fields[label] = value
        eng = _field_key(binding)
        if eng:
            record.raw_fields[eng] = value
    return list(grouped.values())


def _is_diagnosis_source(bindings: list[dict[str, Any]], semantic: dict[str, Any]) -> bool:
    domain = str(semantic.get("domain") or "").lower()
    entity_type = str(semantic.get("entity_type") or "").lower()
    if domain == "diagnosis" or entity_type == "diagnosis":
        return True
    return any(
        _field_key(binding) in {"diagnoseName", "diagTypeDesc", "diagStatusDesc", "diagnoseRemarks"}
        or "诊断名称" in str(binding.get("html_field") or "")
        for binding in bindings or []
        if isinstance(binding, dict)
    )


def _entity_matches(target: str, name: str) -> bool:
    target_text = _normalize(target)
    name_text = _normalize(name)
    if not target_text or not name_text:
        return False
    if target_text in name_text or name_text in target_text:
        return True
    position = 0
    for char in target_text:
        found = name_text.find(char, position)
        if found < 0:
            return False
        position = found + 1
    return True


def _matching_entity(candidates: list[str], name: str) -> str:
    return next((candidate for candidate in candidates if _entity_matches(candidate, name)), "")


def _certainty_decision(record: DiagnosisRecord) -> tuple[Optional[bool], str, str]:
    status_text = " ".join(filter(None, (record.status, record.category)))
    assertion_text = " ".join(filter(None, (record.name, record.status, record.remarks, record.category)))
    if _UNCERTAIN_PATTERN.search(assertion_text):
        return None, "DIAGNOSIS_UNCERTAIN", "诊断记录包含疑似、待排或其他不确定性表述"
    if _EXCLUDED_PATTERN.search(assertion_text) or _REJECTED_STATUS_PATTERN.search(status_text):
        return False, "DIAGNOSIS_EXCLUDED", "诊断记录明确表示已排除、否定或状态无效"
    return True, "DIAGNOSIS_CONFIRMED", "诊断记录未包含疑似、排除或无效状态表述"


def _time_decision(
    record: DiagnosisRecord,
    condition: str,
    time_window: TimeWindow | None,
    temporal_semantics: dict[str, Any],
) -> tuple[Optional[bool], str]:
    if not time_window or not time_window.required:
        return True, "查询未要求诊断时间范围"
    result = filter_bindings_by_time_window(
        record.bindings,
        time_window,
        condition=condition,
        temporal_semantics=temporal_semantics,
    )
    if result.get("applicable") and result.get("matched"):
        return True, str(result.get("reason") or f"诊断记录符合{time_window.scope}时间条件")
    if result.get("applicable"):
        reason = str(result.get("reason") or f"诊断记录不符合{time_window.scope}时间条件")
        if "缺少" in reason or "无法判断" in reason:
            return None, reason
        return False, reason
    return None, f"缺少可用于判断{time_window.scope}的诊断时间或诊断类型"


def _candidate_detail(
    record: DiagnosisRecord,
    certainty: Optional[bool],
    certainty_reason: str,
    in_window: Optional[bool],
    time_reason: str,
    time_window: TimeWindow | None,
    matched_entity: str = "",
) -> dict[str, Any]:
    if time_window and time_window.required:
        if in_window is None:
            scope_status = "UNKNOWN"
        else:
            scope_status = "IN_SCOPE" if in_window else "OUT_OF_SCOPE"
    else:
        scope_status = "NOT_REQUIRED"
    if certainty is True:
        record_status = "MATCHED"
        record_reason_code = "DIAGNOSIS_CONFIRMED"
    elif certainty is False:
        record_status = "NOT_MATCHED"
        record_reason_code = "DIAGNOSIS_EXCLUDED"
    else:
        record_status = "UNKNOWN"
        record_reason_code = "DIAGNOSIS_UNCERTAIN"
    detail: dict[str, Any] = {
        "记录": record.display_reference(),
        "记录序号": f"[{record.prefix}]",
        "记录ID": record.record_id,
        "记录标识名称": record.record_id_label,
        "记录标识字段": record.record_id_field,
        "诊断名称": record.name,
        "诊断类型": record.diagnosis_type,
        "诊断日期": record.diagnosis_date,
        "诊断时间": record.diagnosis_datetime,
        "诊断状态": record.status,
        "诊断备注": record.remarks,
        "诊断分类": record.category,
        "状态是否满足": certainty,
        "确定性判断": certainty_reason,
        "是否在时间窗": in_window,
        "时间判断": time_reason,
        "record_status": record_status,
        "record_reason_code": record_reason_code,
        "record_reason": certainty_reason,
        "scope_status": scope_status,
        "event_time": record.diagnosis_datetime,
    }
    if matched_entity:
        detail["匹配实体"] = matched_entity
    if time_window and time_window.required:
        detail["时间窗"] = time_window.describe() if time_window.resolved else time_window.scope
    return {key: value for key, value in detail.items() if value not in ("", None)}


def judge_diagnosis_condition(
    condition: str,
    bindings: list[dict[str, Any]],
    *,
    entity: str = "",
    entity_candidates: list[str] | None = None,
    time_window: TimeWindow | None = None,
    semantic: dict[str, Any] | None = None,
    temporal_semantics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate literal candidates without replacing semantic synonym matching."""
    semantic = semantic if isinstance(semantic, dict) else {}
    if not _is_diagnosis_source(bindings, semantic):
        return {"applicable": False}

    source_state = " ".join(
        f"{binding.get('html_field', '')} {_field_value(binding)}"
        for binding in bindings or []
        if isinstance(binding, dict)
    )
    if "接口状态" in source_state and any(token in source_state for token in ("未取得", "失败", "超时")):
        return {
            "applicable": True,
            "matched": False,
            "status": "UNKNOWN",
            "reason_code": "SOURCE_UNAVAILABLE",
            "reason": "诊断数据源未取得数据，当前无法判断该诊断条件",
            "fields": source_state[:4000],
            "candidate_count": 0,
            "candidate_records": [],
        }

    targets = normalized_entity_candidates(
        entity,
        {**semantic, "entity_candidates": entity_candidates or semantic.get("entity_candidates") or []},
    )
    target = targets[0] if targets else str(entity or "").strip()
    records = diagnosis_records_from_bindings(bindings)
    matched_entities = {
        id(record): matched_entity
        for record in records
        if (matched_entity := _matching_entity(targets, record.name))
    }
    candidates = [record for record in records if id(record) in matched_entities]
    if not target:
        return {"applicable": False}
    if not candidates:
        return {
            "applicable": True,
            "matched": False,
            "status": "NOT_MENTIONED",
            "reason_code": "NO_MATCHING_RECORD",
            "reason": f"诊断记录中未找到与'{target}'匹配的诊断项",
            "fields": "\n".join(record.compact_line() for record in records[:20]),
            "candidate_count": 0,
            "candidate_records": [],
        }

    evaluated: list[dict[str, Any]] = []
    matched_records: list[DiagnosisRecord] = []
    unknown_records: list[DiagnosisRecord] = []
    excluded_records: list[DiagnosisRecord] = []
    outside_records: list[DiagnosisRecord] = []
    temporal_semantics = temporal_semantics if isinstance(temporal_semantics, dict) else {}
    for record in candidates:
        certainty, _, certainty_reason = _certainty_decision(record)
        in_window, time_reason = _time_decision(
            record,
            condition,
            time_window,
            temporal_semantics,
        )
        evaluated.append(
            _candidate_detail(
                record,
                certainty,
                certainty_reason,
                in_window,
                time_reason,
                time_window,
                matched_entities.get(id(record), target),
            )
        )
        if certainty is True and in_window is True:
            matched_records.append(record)
        elif certainty is None or in_window is None:
            unknown_records.append(record)
        elif certainty is False:
            excluded_records.append(record)
        else:
            outside_records.append(record)

    fields_text = "\n".join(record.compact_line() for record in candidates[:20])
    if matched_records:
        examples = "；".join(
            "，".join(
                [f"{record.display_reference()} 诊断名称={record.name}", f"匹配实体={matched_entities.get(id(record), target)}"]
                + ([f"诊断类型={record.diagnosis_type}"] if record.diagnosis_type else [])
                + ([f"诊断状态={record.status}"] if record.status else [])
                + ([f"诊断时间={record.diagnosis_datetime}"] if record.diagnosis_datetime else [])
            )
            for record in matched_records[:5]
        )
        return {
            "applicable": True,
            "matched": True,
            "status": "MATCHED",
            "reason_code": "DIAGNOSIS_CONFIRMED",
            "reason": f"找到{len(candidates)}条与'{target}'对应的诊断记录，其中{len(matched_records)}条为确定性有效证据：{examples}",
            "fields": fields_text,
            "candidate_count": len(candidates),
            "candidate_records": evaluated,
        }
    if unknown_records:
        return {
            "applicable": True,
            "matched": False,
            "status": "UNKNOWN",
            "reason_code": "DIAGNOSIS_UNCERTAIN",
            "reason": f"找到{len(candidates)}条与'{target}'对应的诊断记录，但其中{len(unknown_records)}条存在疑似、待排或时间证据不足，无法确认诊断成立",
            "fields": fields_text,
            "candidate_count": len(candidates),
            "candidate_records": evaluated,
        }
    if excluded_records:
        reason_code = "DIAGNOSIS_EXCLUDED"
        reason = f"找到{len(candidates)}条与'{target}'对应的诊断记录，但均为排除、否定或无效状态"
    else:
        reason_code = "TIME_OUTSIDE_WINDOW"
        reason = f"找到{len(candidates)}条与'{target}'对应的诊断记录，但均不在目标时间范围内"
    return {
        "applicable": True,
        "matched": False,
        "status": "NOT_MATCHED",
        "reason_code": reason_code,
        "reason": reason,
        "fields": fields_text,
        "candidate_count": len(candidates),
        "candidate_records": evaluated,
    }
