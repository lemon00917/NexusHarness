"""Deterministic structured medication evidence evaluation.

The LLM/query planner identifies the medication concept and intent. This module
maps service fields to canonical roles and owns record, time-window, and
three-state evidence decisions. It intentionally contains no drug-specific
branches.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from microharness.medical.entity_normalization import entity_candidates as normalized_entity_candidates
from microharness.medical.record_identity import display_record_reference, identity_from_binding
from microharness.medical.time_window import TimeWindow, parse_datetime_value


DEFAULT_FIELD_ROLES: dict[str, tuple[str, ...]] = {
    "record_id": ("hdcOrdId", "hosOrdId", "医嘱号", "记录ID"),
    "prescription_no": ("medPrescNo", "处方号"),
    "entity": ("orderName", "药物名称", "药品名称", "医嘱名称", "医嘱项"),
    "category": ("ordCatDesc", "ordSubCatDesc", "医嘱大类", "医嘱子类", "用药类型"),
    "ordered_at": ("开立日期时间", "orderDateTime", "orderDate", "开立时间", "开立日期"),
    "administered_at": (
        "administeredAt", "administrationDateTime", "executeDateTime",
        "给药日期时间", "给药时间", "执行日期时间", "执行时间",
    ),
    "start_at": ("startDateTime", "医嘱开始时间", "开始时间"),
    "end_at": ("endDateTime", "医嘱结束时间", "结束时间", "停止时间"),
    "status_code": ("ordStatusCode", "orderStatusCode", "executeStatusCode", "医嘱状态编码", "执行状态编码"),
    "status": ("ordStatusDesc", "orderStatus", "executeStatus", "医嘱状态", "执行状态", "给药状态"),
    "dose": ("medicineDosage", "单次剂量", "剂量"),
    "dose_unit": ("medDosUnitDesc", "剂量单位"),
    "frequency": ("medFreqDesc", "频次", "用药频次"),
    "route": ("medUsageDesc", "用药途径", "给药途径"),
    "form": ("medDoseFormDesc", "剂型"),
    "duration": ("medDurDesc", "疗程"),
    "quantity": ("orderQuantity", "数量"),
    "remarks": ("orderRemarks", "医嘱备注", "备注"),
}


@dataclass
class MedicationRecord:
    prefix: str
    record_id: str = ""
    record_id_label: str = ""
    record_id_field: str = ""
    values: dict[str, str] = field(default_factory=dict)
    raw_fields: list[dict[str, str]] = field(default_factory=list)

    def get(self, role: str) -> str:
        return str(self.values.get(role, "") or "").strip()

    def event_time(self, predicate: str, preferred_role: str = "") -> tuple[Optional[datetime], str, str]:
        if preferred_role:
            role_order = (preferred_role,)
        elif predicate == "administered":
            role_order = ("administered_at", "start_at")
        elif predicate == "stopped":
            role_order = ("end_at",)
        else:
            role_order = ("ordered_at", "start_at")
        for role in role_order:
            raw = self.get(role)
            parsed = parse_datetime_value(raw)
            if parsed:
                return parsed, role, raw
        return None, "", ""

    def compact_line(self) -> str:
        parts = []
        for item in self.raw_fields:
            label = item.get("label", "")
            value = item.get("value", "")
            if label and value:
                parts.append(f"{label}: {value}")
        return f"  {self.prefix} " + " | ".join(parts)

    def display_reference(self) -> str:
        return display_record_reference(self.prefix, self.record_id, self.record_id_label)


def _split_prefixed_label(label: str) -> tuple[str, str]:
    match = re.match(r"\s*(\[[^\]]+\])\s*(.*)", str(label or ""))
    if match:
        return match.group(1), match.group(2).strip()
    return "[记录]", str(label or "").strip()


def _normalize(value: str) -> str:
    return re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", str(value or "")).lower()


def _field_roles(semantic_fields: dict[str, Any] | None) -> dict[str, tuple[str, ...]]:
    configured = semantic_fields if isinstance(semantic_fields, dict) else {}
    roles: dict[str, tuple[str, ...]] = {}
    for role, defaults in DEFAULT_FIELD_ROLES.items():
        aliases = configured.get(role, ())
        if isinstance(aliases, str):
            aliases = (aliases,)
        elif not isinstance(aliases, (list, tuple, set)):
            aliases = ()
        roles[role] = tuple(dict.fromkeys(str(v) for v in (*aliases, *defaults) if str(v).strip()))
    return roles


def _matches_alias(label: str, eng: str, aliases: tuple[str, ...]) -> bool:
    label_norm = _normalize(label)
    eng_norm = _normalize(eng)
    for alias in aliases:
        alias_norm = _normalize(alias)
        if not alias_norm:
            continue
        if alias_norm == eng_norm or alias_norm == label_norm or alias_norm in label_norm:
            return True
    return False


def parse_medication_records(
    bindings: list[dict[str, Any]],
    semantic_fields: dict[str, Any] | None = None,
) -> list[MedicationRecord]:
    roles = _field_roles(semantic_fields)
    grouped: dict[str, list[dict[str, str]]] = {}
    for binding in bindings or []:
        prefix, label = _split_prefixed_label(str(binding.get("html_field") or ""))
        value = str(binding.get("html_value") or binding.get("value") or "").strip()
        if not value:
            continue
        eng = str(binding.get("eng_field") or binding.get("xml_path", "").split("/")[-1] or "")
        identity = identity_from_binding(binding)
        grouped.setdefault(prefix, []).append({
            "label": label,
            "eng": eng,
            "value": value,
            **identity,
        })

    records = []
    for prefix, fields in grouped.items():
        values: dict[str, str] = {}
        for role, aliases in roles.items():
            matches = [item["value"] for item in fields if _matches_alias(item["label"], item["eng"], aliases)]
            if matches:
                values[role] = "；".join(dict.fromkeys(matches))
        if values.get("entity") or values.get("category"):
            identity = next((item for item in fields if item.get("record_id")), {})
            records.append(MedicationRecord(
                prefix=prefix,
                record_id=str(identity.get("record_id") or values.get("record_id") or ""),
                record_id_label=str(identity.get("record_id_label") or ""),
                record_id_field=str(identity.get("record_id_field") or ""),
                values=values,
                raw_fields=fields,
            ))
    return records


def infer_medication_predicate(condition: str, configured: str = "") -> str:
    text = str(condition or "")
    if re.search(r"(停用|停止|停药|终止)", text):
        return "stopped"
    if re.search(r"(开立|开过|开了|医嘱|处方)", text) and not re.search(r"(使用|服用|吃过|吃了|给药|执行|注射|输注|用过)", text):
        return "ordered"
    if re.search(r"(使用|服用|吃过|吃了|给药|执行|注射|输注|用过)", text):
        return "administered"
    configured = str(configured or "").strip().lower()
    return configured if configured in {"ordered", "administered", "stopped"} else "administered"


def _entity_matches(target: str, record: MedicationRecord) -> bool:
    target_norm = _normalize(target)
    if not target_norm or target_norm in {"用药", "药物", "药品", "医嘱", "处方"}:
        return True
    candidate = _normalize(record.get("entity") + record.get("category"))
    if not candidate:
        return False
    if target_norm in candidate or candidate in target_norm:
        return True
    target_chars = set(target_norm)
    if len(target_chars) < 3:
        return False
    return len(target_chars & set(candidate)) / len(target_chars) >= 0.8


def _matching_entity(candidates: list[str], record: MedicationRecord) -> str:
    return next((candidate for candidate in candidates if _entity_matches(candidate, record)), "")


def _capability(capabilities: dict[str, Any] | None, name: str, default: bool) -> bool:
    if not isinstance(capabilities, dict) or name not in capabilities:
        return default
    return bool(capabilities.get(name))


def _policy_values(policy: dict[str, Any], key: str) -> set[str]:
    values = policy.get(key, []) if isinstance(policy, dict) else []
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple, set)):
        return set()
    return {_normalize(value) for value in values if _normalize(value)}


def _display_status(record: MedicationRecord) -> str:
    description = record.get("status")
    code = record.get("status_code")
    if description and code:
        return f"{description}({code})"
    return description or code or "未取得"


def _status_decision(record: MedicationRecord, policy: dict[str, Any]) -> tuple[Optional[bool], str]:
    if not policy.get("required_status"):
        return True, "该判断口径不要求医嘱状态"
    code = _normalize(record.get("status_code"))
    desc = _normalize(record.get("status"))
    accepted = _policy_values(policy, "accepted_status_codes") | _policy_values(policy, "accepted_status_values")
    rejected = _policy_values(policy, "rejected_status_codes") | _policy_values(policy, "rejected_status_values")
    displayed = _display_status(record)
    if code in rejected or desc in rejected:
        return False, f"医嘱状态={displayed}，属于无效状态"
    if code in accepted or desc in accepted:
        return True, f"医嘱状态={displayed}，属于有效状态"
    if not code and not desc:
        return None, "缺少医嘱状态，无法判断医嘱是否有效"
    return None, f"医嘱状态={displayed}未配置有效性规则，无法判断"


def _candidate_detail(
    record: MedicationRecord,
    predicate: str,
    event_time: Optional[datetime],
    event_role: str,
    in_window: Optional[bool],
    time_window: TimeWindow | None,
    reason: str,
) -> dict[str, Any]:
    if time_window and time_window.required:
        if in_window is None:
            scope_status = "UNKNOWN"
        else:
            scope_status = "IN_SCOPE" if in_window else "OUT_OF_SCOPE"
    else:
        scope_status = "NOT_REQUIRED"
    detail = {
        "记录": record.display_reference(),
        "记录序号": record.prefix,
        "记录ID": record.record_id,
        "记录标识名称": record.record_id_label,
        "记录标识字段": record.record_id_field,
        "项目": record.get("entity") or record.get("category"),
        "医嘱项": record.get("entity"),
        "医嘱号": record.record_id,
        "处方号": record.get("prescription_no"),
        "开立时间": record.get("ordered_at"),
        "执行/给药时间": record.get("administered_at") or record.get("start_at"),
        "记录时间": event_time.strftime("%Y-%m-%d %H:%M:%S") if event_time else "未取得",
        "证据时间角色": event_role,
        "证据谓词": predicate,
        "是否在时间窗": in_window,
        "时间窗": time_window.describe() if time_window and time_window.resolved else "",
        "时间判断": reason,
        "医嘱状态": record.get("status"),
        "医嘱状态编码": record.get("status_code"),
        "剂量": record.get("dose"),
        "剂量单位": record.get("dose_unit"),
        "频次": record.get("frequency"),
        "途径": record.get("route"),
        "剂型": record.get("form"),
        "疗程": record.get("duration"),
        "数量": record.get("quantity"),
        "备注": record.get("remarks"),
        "record_status": "UNKNOWN",
        "record_reason_code": "INSUFFICIENT_EVIDENCE",
        "record_reason": reason,
        "scope_status": scope_status,
        "event_time": event_time.strftime("%Y-%m-%d %H:%M:%S") if event_time else "",
    }
    return {key: value for key, value in detail.items() if value not in ("", None)}


def judge_medication_condition(
    condition: str,
    bindings: list[dict[str, Any]],
    *,
    entity: str = "",
    entity_candidates: list[str] | None = None,
    time_window: TimeWindow | None = None,
    semantic: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate one medication condition and return legacy-compatible evidence."""
    semantic = semantic if isinstance(semantic, dict) else {}
    if str(semantic.get("domain") or "").lower() != "medication" and str(semantic.get("entity_type") or "").lower() != "drug":
        return {"applicable": False}

    source_state = " ".join(
        f"{binding.get('html_field', '')} {binding.get('html_value') or binding.get('value') or ''}"
        for binding in bindings or []
    )
    if "接口状态" in source_state and any(token in source_state for token in ("未取得", "失败", "超时")):
        return {
            "applicable": True, "matched": False, "status": "UNKNOWN",
            "reason_code": "SOURCE_UNAVAILABLE",
            "reason": "用药数据源未取得数据，当前无法判断该用药条件",
            "fields": source_state[:4000], "candidate_count": 0, "candidate_records": [],
        }

    predicate = infer_medication_predicate(condition, str(semantic.get("predicate") or ""))
    fields = semantic.get("fields") if isinstance(semantic.get("fields"), dict) else {}
    capabilities = semantic.get("evidence_capabilities") if isinstance(semantic.get("evidence_capabilities"), dict) else {}
    predicate_policies = semantic.get("predicate_policies") if isinstance(semantic.get("predicate_policies"), dict) else {}
    predicate_policy = predicate_policies.get(predicate) if isinstance(predicate_policies.get(predicate), dict) else {}
    records = parse_medication_records(bindings, fields)
    targets = normalized_entity_candidates(
        entity,
        {**semantic, "entity_candidates": entity_candidates or semantic.get("entity_candidates") or []},
    )
    matched_entities = {
        id(record): matched_entity
        for record in records
        if (matched_entity := _matching_entity(targets, record))
    }
    candidates = [record for record in records if id(record) in matched_entities]
    target = targets[0] if targets else entity or "目标用药"

    if not candidates:
        return {
            "applicable": True, "matched": False, "status": "NOT_MENTIONED",
            "reason_code": "NO_MATCHING_RECORD",
            "reason": f"用药记录中未找到与'{target}'匹配的医嘱项",
            "fields": "\n".join(record.compact_line() for record in records[:20]),
            "candidate_count": 0, "candidate_records": [],
        }

    supports_administered = _capability(capabilities, "administered", False)
    if predicate == "administered" and not supports_administered and not predicate_policy:
        details = []
        for record in candidates:
            ordered_time, ordered_role, _ = record.event_time("ordered")
            detail = _candidate_detail(record, predicate, ordered_time, ordered_role, None, time_window, "当前数据源仅提供开立医嘱，不能证明实际执行或给药")
            detail["匹配实体"] = matched_entities.get(id(record), target)
            if time_window and time_window.resolved and ordered_time:
                ordered_in_window = time_window.contains(ordered_time)
                detail["开立时间是否在时间窗"] = ordered_in_window
                detail["开立时间判断"] = (
                    f"开立时间{ordered_time.strftime('%Y-%m-%d %H:%M:%S')}"
                    f"{'在' if ordered_in_window else '不在'}{time_window.scope}（范围：{time_window.describe()}），"
                    "但开立时间不能代替执行/给药时间"
                )
            details.append(detail)
        return {
            "applicable": True, "matched": False, "status": "UNKNOWN",
            "reason_code": "INSUFFICIENT_EVIDENCE",
            "reason": f"找到{len(candidates)}条与'{target}'匹配的医嘱项，但当前数据源仅提供开立信息，不能据此确认患者实际使用、执行或给药",
            "fields": "\n".join(record.compact_line() for record in candidates[:20]),
            "candidate_count": len(candidates), "candidate_records": details,
        }

    if time_window and time_window.required and not time_window.resolved:
        details = []
        for record in candidates:
            detail = _candidate_detail(record, predicate, None, "", None, time_window, "缺少事件时间锚点，无法计算目标时间窗")
            detail["匹配实体"] = matched_entities.get(id(record), target)
            details.append(detail)
        return {
            "applicable": True, "matched": False, "status": "UNKNOWN",
            "reason_code": "MISSING_EVENT_TIME",
            "reason": f"找到{len(candidates)}条与'{target}'匹配的记录，但缺少事件时间锚点，无法判断是否位于{time_window.scope}",
            "fields": "\n".join(record.compact_line() for record in candidates[:20]),
            "candidate_count": len(candidates), "candidate_records": details,
        }

    evaluated = []
    matched_records = []
    unknown_records = []
    rejected_records = []
    outside_records = []
    preferred_time_role = str(predicate_policy.get("event_time_role") or "")
    for record in candidates:
        event_time, event_role, _ = record.event_time(predicate, preferred_time_role)
        status_ok, status_reason = _status_decision(record, predicate_policy)
        if time_window and time_window.required:
            if not event_time:
                in_window = None
                reason = "缺少可用于该判断的记录时间"
            else:
                in_window = time_window.contains(event_time)
                reason = f"{event_time.strftime('%Y-%m-%d %H:%M:%S')}{'在' if in_window else '不在'}{time_window.scope}（范围：{time_window.describe()}）"
        else:
            in_window = True
            reason = "记录提供了与查询谓词相符的结构化证据"

        if status_ok is False:
            rejected_records.append(record)
        elif status_ok is None or in_window is None:
            unknown_records.append(record)
        elif in_window:
            matched_records.append(record)
        else:
            outside_records.append(record)
        detail = _candidate_detail(record, predicate, event_time, event_role, in_window, time_window, reason)
        detail["匹配实体"] = matched_entities.get(id(record), target)
        detail["状态是否满足"] = status_ok
        detail["状态判断"] = status_reason
        if status_ok is True:
            detail["record_status"] = "MATCHED"
            detail["record_reason_code"] = "STATUS_CONDITION_MET"
        elif status_ok is False:
            detail["record_status"] = "NOT_MATCHED"
            detail["record_reason_code"] = "STATUS_CONDITION_NOT_MET"
        else:
            detail["record_status"] = "UNKNOWN"
            detail["record_reason_code"] = "STATUS_CONDITION_UNKNOWN"
        detail["record_reason"] = status_reason
        if predicate_policy:
            detail["业务判定口径"] = "开立时间和医嘱状态" if preferred_time_role == "ordered_at" else "记录时间和医嘱状态"
        evaluated.append(detail)

    fields_text = "\n".join(record.compact_line() for record in candidates[:20])
    if matched_records:
        names = '；'.join(
            '{} 医嘱项={}，匹配实体={}，开立时间={}，途径={}，医嘱状态={}'.format(
                record.display_reference(), record.get('entity'), matched_entities.get(id(record), target),
                record.get('ordered_at') or '未取得',
                record.get('route') or '未取得', _display_status(record),
            ) for record in matched_records[:5]
        )
        return {
            "applicable": True, "matched": True, "status": "MATCHED",
            "reason_code": "MATCH_CONFIRMED",
            "reason": f"找到{len(candidates)}条与'{target}'匹配的记录，其中{len(matched_records)}条符合：{names}",
            "fields": fields_text, "candidate_count": len(candidates), "candidate_records": evaluated,
        }
    if unknown_records:
        return {
            "applicable": True, "matched": False, "status": "UNKNOWN",
            "reason_code": "INSUFFICIENT_EVIDENCE",
            "reason": f"找到{len(candidates)}条与'{target}'匹配的医嘱项，但{len(unknown_records)}条缺少可判定的开立时间或医嘱状态，无法完整判断",
            "fields": fields_text, "candidate_count": len(candidates), "candidate_records": evaluated,
        }
    detail_reason = '；'.join(
        '{} 医嘱项={}，开立时间={}，途径={}，{}，{}'.format(
            item.get('记录'), item.get('医嘱项'), item.get('开立时间') or '未取得',
            item.get('途径') or '未取得', item.get('时间判断'), item.get('状态判断'),
        ) for item in evaluated[:5]
    )
    return {
        "applicable": True, "matched": False, "status": "NOT_MATCHED",
        "reason_code": "STATUS_CONDITION_NOT_MET" if rejected_records else "TIME_OUTSIDE_WINDOW",
        "reason": f"找到{len(candidates)}条与'{target}'匹配的医嘱项，但均不符合开立时间和医嘱状态条件：{detail_reason}",
        "fields": fields_text, "candidate_count": len(candidates), "candidate_records": evaluated,
    }
