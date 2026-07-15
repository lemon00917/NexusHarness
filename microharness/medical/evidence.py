"""
Structured evidence helpers for medical filter results.

The existing API returns string evidence for compatibility. These helpers add
a Chinese-key structured form that can be rendered by clients without parsing
free text.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
import json
import re
from typing import Any

from microharness.medical.display_text import sanitize_user_text


class EvidenceStatus(str, Enum):
    MATCHED = "MATCHED"
    NOT_MATCHED = "NOT_MATCHED"
    UNKNOWN = "UNKNOWN"


class DataQuality(str, Enum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    MISSING = "MISSING"
    SOURCE_ERROR = "SOURCE_ERROR"


class ReasonCode(str, Enum):
    MATCH_CONFIRMED = "MATCH_CONFIRMED"
    NO_MATCHING_RECORD = "NO_MATCHING_RECORD"
    TIME_OUTSIDE_WINDOW = "TIME_OUTSIDE_WINDOW"
    MISSING_EVENT_TIME = "MISSING_EVENT_TIME"
    VALUE_CONDITION_NOT_MET = "VALUE_CONDITION_NOT_MET"
    STATUS_CONDITION_NOT_MET = "STATUS_CONDITION_NOT_MET"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


@dataclass
class EvidenceItem:
    """Canonical machine-readable evidence emitted by every source adapter."""

    condition_id: str
    source_type: str
    source_name: str
    record_id: str = ""
    document: str = ""
    section: str = ""
    entity: str = ""
    raw_text: str = ""
    event_time: Any = None
    value: Any = None
    unit: str = ""
    abnormal_flag: str = ""
    reference_range: str = ""
    status: EvidenceStatus = EvidenceStatus.UNKNOWN
    reason_code: ReasonCode = ReasonCode.INSUFFICIENT_EVIDENCE
    data_quality: DataQuality = DataQuality.MISSING
    reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        data["reason_code"] = self.reason_code.value
        data["data_quality"] = self.data_quality.value
        return data


@dataclass
class ConditionResult:
    """Canonical three-state result for one atomic condition."""

    condition_id: str
    condition: str
    status: EvidenceStatus
    reason_code: ReasonCode
    reason: str
    data_quality: DataQuality
    evidence: list[EvidenceItem] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "condition_id": self.condition_id,
            "condition": self.condition,
            "status": self.status.value,
            "matched": self.status == EvidenceStatus.MATCHED,
            "conclusive": self.status != EvidenceStatus.UNKNOWN,
            "reason_code": self.reason_code.value,
            "reason": self.reason,
            "data_quality": self.data_quality.value,
            "evidence": [item.to_dict() for item in self.evidence],
        }


@dataclass
class EvidenceDisplayItem:
    来源: str
    结论: str
    理由: str
    原文: str
    证据级别: str = "候选证据"

    def to_dict(self) -> dict:
        return asdict(self)


def classify_evidence(matched: bool, reason: str) -> str:
    reason = reason or ""
    if matched:
        return "支持证据"
    if any(token in reason for token in ("不符合", "未找到", "未出现", "无匹配", "无法判断", "不满足")):
        return "反证"
    return "候选证据"


def build_evidence_items(file_results: list[dict]) -> list[dict]:
    items = []
    for file_result in file_results or []:
        matched = bool(file_result.get("matched", False))
        reason = sanitize_user_text(str(file_result.get("reason", "")))
        fields = str(file_result.get("fields", ""))
        role = str(file_result.get("证据角色") or file_result.get("evidence_role") or "")
        if role and role != "主证据":
            conclusion = "辅助依据"
            level = role
        else:
            conclusion = "符合" if matched else "不符合"
            level = classify_evidence(matched, reason)
        confidence = assess_file_confidence(file_result)
        item = EvidenceDisplayItem(
            来源=str(file_result.get("file", "")),
            结论=conclusion,
            理由=reason,
            原文=fields,
            证据级别=level,
        ).to_dict() | confidence
        if role:
            item["证据角色"] = role
        if file_result.get("用途"):
            item["用途"] = str(file_result.get("用途"))
        items.append(item)
    return items


UNKNOWN_MARKERS = (
    "无法判断",
    "接口失败",
    "请求超时",
    "外部数据源调用失败",
    "DB不可用",
    "数据库不可用",
    "未取得数据",
    "未取得病历",
    "未取得接口",
    "未找到日期字段",
    "文件中无相关日期/数值字段",
    "不能据此确认",
    "证据不足",
)

SOURCE_ERROR_MARKERS = (
    "接口失败",
    "请求超时",
    "外部数据源调用失败",
    "DB不可用",
    "数据库不可用",
)


def _first_value(data: dict[str, Any], *keys: str, default: Any = "") -> Any:
    for key in keys:
        value = data.get(key)
        if value is not None and value != "":
            return value
    return default


def _evidence_status(file_result: dict[str, Any]) -> EvidenceStatus:
    explicit = str(_first_value(file_result, "status", "判断状态")).upper()
    status_map = {
        "MATCHED": EvidenceStatus.MATCHED,
        "符合": EvidenceStatus.MATCHED,
        "NOT_MATCHED": EvidenceStatus.NOT_MATCHED,
        "不符合": EvidenceStatus.NOT_MATCHED,
        "UNKNOWN": EvidenceStatus.UNKNOWN,
        "无法判断": EvidenceStatus.UNKNOWN,
    }
    if explicit in status_map:
        return status_map[explicit]
    reason = str(file_result.get("reason", "") or "")
    if any(marker in reason for marker in UNKNOWN_MARKERS):
        return EvidenceStatus.UNKNOWN
    return EvidenceStatus.MATCHED if bool(file_result.get("matched", False)) else EvidenceStatus.NOT_MATCHED


def _reason_code(status: EvidenceStatus, reason: str, explicit: Any = "") -> ReasonCode:
    explicit_text = str(explicit or "").upper()
    if explicit_text in ReasonCode._value2member_map_:
        return ReasonCode(explicit_text)
    if any(marker in reason for marker in SOURCE_ERROR_MARKERS):
        return ReasonCode.SOURCE_UNAVAILABLE
    if any(marker in reason for marker in ("未找到日期字段", "无相关日期", "缺少事件时间")):
        return ReasonCode.MISSING_EVENT_TIME
    if any(marker in reason for marker in ("不在事件前时间窗", "不在时间窗", "超出时间范围")):
        return ReasonCode.TIME_OUTSIDE_WINDOW
    if any(marker in reason for marker in ("数值条件不符合", "结果不符合", "不满足数值")):
        return ReasonCode.VALUE_CONDITION_NOT_MET
    if any(marker in reason for marker in ("医嘱状态", "无效状态", "状态不符合")):
        return ReasonCode.STATUS_CONDITION_NOT_MET
    if status == EvidenceStatus.MATCHED:
        return ReasonCode.MATCH_CONFIRMED
    if status == EvidenceStatus.NOT_MATCHED:
        return ReasonCode.NO_MATCHING_RECORD
    return ReasonCode.INSUFFICIENT_EVIDENCE


def _data_quality(file_result: dict[str, Any], status: EvidenceStatus) -> DataQuality:
    explicit = str(file_result.get("data_quality", "") or "").upper()
    if explicit in DataQuality._value2member_map_:
        return DataQuality(explicit)
    reason = str(file_result.get("reason", "") or "")
    if any(marker in reason for marker in SOURCE_ERROR_MARKERS):
        return DataQuality.SOURCE_ERROR
    raw_text = str(_first_value(file_result, "raw_text", "fields", "原文") or "").strip()
    has_structured_value = any(
        file_result.get(key) not in (None, "")
        for key in ("event_time", "record_time", "value", "unit", "abnormal_flag", "reference_range")
    )
    if not raw_text and not has_structured_value:
        return DataQuality.MISSING if status == EvidenceStatus.UNKNOWN else DataQuality.PARTIAL
    if status == EvidenceStatus.UNKNOWN:
        return DataQuality.PARTIAL
    return DataQuality.COMPLETE


def adapt_legacy_evidence(file_result: dict[str, Any], condition_id: str) -> EvidenceItem:
    """Convert one existing file/service result without changing its judgement."""
    reason = sanitize_user_text(str(file_result.get("reason", "") or ""))
    status = _evidence_status(file_result)
    source_type = str(file_result.get("source_type", "") or "")
    if not source_type:
        if any(file_result.get(key) for key in ("document", "doc", "section", "template")):
            source_type = "document"
        elif any(file_result.get(key) for key in ("service", "skill", "service_id")):
            source_type = "service"
        else:
            source_type = "legacy_result"
    metadata = {
        key: file_result[key]
        for key in ("evidence_role", "证据角色", "purpose", "用途")
        if file_result.get(key) not in (None, "")
    }
    return EvidenceItem(
        condition_id=condition_id,
        source_type=source_type,
        source_name=str(_first_value(file_result, "source_name", "file", "service", "document")),
        record_id=str(_first_value(file_result, "record_id", "recordId", "id")),
        document=str(_first_value(file_result, "document", "doc", "template")),
        section=str(_first_value(file_result, "section", "section_name")),
        entity=str(_first_value(file_result, "entity", "keyword", "item_name")),
        raw_text=str(_first_value(file_result, "raw_text", "fields", "原文")),
        event_time=_first_value(file_result, "event_time", "record_time", "time", default=None),
        value=_first_value(file_result, "value", "result_value", default=None),
        unit=str(_first_value(file_result, "unit", "result_unit")),
        abnormal_flag=str(_first_value(file_result, "abnormal_flag", "abnormalFlag")),
        reference_range=str(_first_value(file_result, "reference_range", "referenceRange")),
        status=status,
        reason_code=_reason_code(status, reason, file_result.get("reason_code")),
        data_quality=_data_quality(file_result, status),
        reason=reason,
        metadata=metadata,
    )


def _binding_parts(binding: dict[str, Any]) -> tuple[str, str, str, str]:
    label = str(binding.get("html_field", "") or "").strip()
    value = str(binding.get("html_value") or binding.get("value", "") or "").strip()
    path = str(binding.get("xml_path", "") or "").strip()
    matched = re.match(r"^\[([^\]]+)\]\s*(.*)$", label)
    if matched:
        return matched.group(1).strip(), matched.group(2).strip(), value, path
    return "", label, value, path


def _source_type(source: dict[str, Any]) -> str:
    semantic = source.get("semantic") if isinstance(source.get("semantic"), dict) else {}
    entity_type = str(semantic.get("entity_type", "") or "").strip().lower()
    if entity_type in {"diagnosis", "encounter"}:
        return entity_type
    bindings = [item for item in source.get("bindings", []) or [] if isinstance(item, dict)]
    paths = [str(item.get("xml_path", "") or "") for item in bindings]
    if source.get("service_id") or (paths and all(path.startswith("external/") for path in paths)):
        return "service"
    return "document"


def _record_event_time(fields: dict[str, Any]) -> Any:
    for label in ("检测时间", "记录时间", "开立时间", "执行/给药时间", "入院日期时间", "出院日期时间"):
        if fields.get(label):
            return fields[label]
    date_value = next((v for k, v in fields.items() if k.endswith("日期") and v), "")
    time_value = next((v for k, v in fields.items() if k.endswith("时间") and v), "")
    return f"{date_value} {time_value}".strip() or None


def _entity_overlaps(entity: str, text: str) -> bool:
    entity_chars = [char.lower() for char in str(entity) if char.isalnum()]
    if not entity_chars:
        return False
    normalized_text = str(text).lower()
    if str(entity).lower() in normalized_text:
        return True
    return sum(char in normalized_text for char in set(entity_chars)) / len(set(entity_chars)) >= 0.7


def attach_native_evidence_records(file_result, source, **options):
    """Attach native records while retaining all legacy result fields."""
    if not isinstance(file_result, dict) or not isinstance(source, dict):
        return file_result
    if source.get("service_error") or file_result.get("候选记录") or file_result.get("candidate_records"):
        return file_result
    source_type = _source_type(source)
    if source_type not in {"diagnosis", "encounter", "document"}:
        return file_result
    bindings = [item for item in source.get("bindings", []) or [] if isinstance(item, dict)]
    if not bindings:
        return file_result
    source_name = str(file_result.get("file") or source.get("file") or "")
    template = str(source.get("template", "") or "")
    status = _evidence_status(file_result)
    reason = sanitize_user_text(str(file_result.get("reason", "") or ""))
    common = {"source_type": source_type, "source_name": source_name,
              "document": template, "status": status.value,
              "reason_code": _reason_code(status, reason).value, "reason": reason}
    records = []
    if source_type in {"diagnosis", "encounter"}:
        grouped = {}
        for binding in bindings:
            record_id, field_name, value, path = _binding_parts(binding)
            if value:
                grouped.setdefault(record_id or "记录1", []).append((field_name, value, path))
        for record_id, parts in grouped.items():
            fields = {name: value for name, value, _ in parts}
            entity = next((v for k, v in fields.items() if k.endswith("名称")), "")
            if not entity:
                entity = next((fields.get(k) for k in ("就诊类型", "就诊科室", "就诊状态", "当前病区") if fields.get(k)), options.get("entity", ""))
            records.append(common | {"record_id": record_id, "entity": entity,
                "raw_text": json.dumps(fields, ensure_ascii=False),
                "event_time": _record_event_time(fields), "data_quality": "COMPLETE",
                "metadata": {"service_id": source.get("service_id", ""),
                    "semantic": source.get("semantic", {}), "condition": options.get("condition", ""), "fields": fields,
                    "field_paths": {name: path for name, _, path in parts}}})
        target_entity = str(options.get("entity", "") or "")
        if source_type == "diagnosis" and status == EvidenceStatus.MATCHED and target_entity:
            related = [item for item in records if _entity_overlaps(target_entity, item.get("raw_text", ""))]
            records = related
    else:
        targets = [str(item) for item in options.get("target_sections", [])]
        xml_targets = [str(item) for item in options.get("target_xml", [])]
        for binding in bindings:
            _, field_name, value, path = _binding_parts(binding)
            xml_tag = path.split("/")[-1] if path else ""
            selected = not (targets or xml_targets) or any(
                t in field_name or field_name in t or t in path or t in xml_tag or xml_tag in t
                for t in targets + xml_targets)
            if value and selected:
                records.append(common | {"record_id": source_name, "document": source_name,
                    "section": field_name, "entity": options.get("entity", ""),
                    "raw_text": value, "value": value, "data_quality": "COMPLETE",
                    "metadata": {"template": template, "condition": options.get("condition", ""),
                                 "xml_path": path, "field_name": field_name}})
        target_entity = str(options.get("entity", "") or "")
        if status == EvidenceStatus.MATCHED and target_entity:
            related = [item for item in records if _entity_overlaps(target_entity, item.get("raw_text", ""))]
            role_based_numeric_evidence = bool(options.get("is_numeric") and (targets or xml_targets))
            if related:
                records = related
            elif not role_based_numeric_evidence:
                records = []
    if records:
        file_result["_structured_evidence_records"] = records
    return file_result


def _structured_record_evidence(file_result, record, condition_id):
    parent = adapt_legacy_evidence(file_result, condition_id)
    status_text = str(record.get("status", "")).upper()
    status = EvidenceStatus(status_text) if status_text in EvidenceStatus._value2member_map_ else parent.status
    code_text = str(record.get("reason_code", "")).upper()
    reason = sanitize_user_text(str(record.get("reason") or parent.reason))
    code = ReasonCode(code_text) if code_text in ReasonCode._value2member_map_ else _reason_code(status, reason)
    quality_text = str(record.get("data_quality", "")).upper()
    quality = DataQuality(quality_text) if quality_text in DataQuality._value2member_map_ else parent.data_quality
    metadata = dict(parent.metadata)
    metadata.update(record.get("metadata") if isinstance(record.get("metadata"), dict) else {})
    return EvidenceItem(condition_id=condition_id,
        source_type=str(record.get("source_type") or parent.source_type),
        source_name=str(record.get("source_name") or parent.source_name),
        record_id=str(record.get("record_id") or parent.record_id),
        document=str(record.get("document") or parent.document),
        section=str(record.get("section") or parent.section), entity=str(record.get("entity") or parent.entity),
        raw_text=str(record.get("raw_text") or parent.raw_text),
        event_time=record.get("event_time", parent.event_time), value=record.get("value", parent.value),
        unit=str(record.get("unit") or parent.unit), abnormal_flag=str(record.get("abnormal_flag") or parent.abnormal_flag),
        reference_range=str(record.get("reference_range") or parent.reference_range),
        status=status, reason_code=code, data_quality=quality, reason=reason, metadata=metadata)


def _candidate_record_evidence(
    file_result: dict[str, Any],
    record: dict[str, Any],
    condition_id: str,
) -> EvidenceItem:
    parent = adapt_legacy_evidence(file_result, condition_id)
    in_window = _first_value(record, "是否在时间窗", "in_time_window", default=None)
    value_satisfied = _first_value(record, "数值是否满足", "value_satisfied", default=None)
    status_satisfied = _first_value(record, "状态是否满足", "status_satisfied", default=None)
    has_status_constraint = "状态是否满足" in record or "status_satisfied" in record
    event_time = _first_value(
        record,
        "检测时间",
        "记录时间",
        "执行/给药时间",
        "开立时间",
        "event_time",
        "time",
        default=None,
    )

    if event_time in ("未取得", "缺少检测时间"):
        status = EvidenceStatus.UNKNOWN
        reason_code = ReasonCode.MISSING_EVENT_TIME
    elif has_status_constraint and status_satisfied is None:
        status = EvidenceStatus.UNKNOWN
        reason_code = ReasonCode.INSUFFICIENT_EVIDENCE
    elif status_satisfied is False:
        status = EvidenceStatus.NOT_MATCHED
        reason_code = ReasonCode.STATUS_CONDITION_NOT_MET
    elif in_window is False:
        status = EvidenceStatus.NOT_MATCHED
        reason_code = ReasonCode.TIME_OUTSIDE_WINDOW
    elif value_satisfied is False:
        status = EvidenceStatus.NOT_MATCHED
        reason_code = ReasonCode.VALUE_CONDITION_NOT_MET
    elif in_window is True or value_satisfied is True:
        status = EvidenceStatus.MATCHED
        reason_code = ReasonCode.MATCH_CONFIRMED
    else:
        status = parent.status
        reason_code = parent.reason_code

    reason_parts = []
    time_reason = _first_value(record, "时间判断", "time_reason")
    value_reason = _first_value(record, "数值判断", "value_reason")
    status_reason = _first_value(record, "状态判断", "status_reason")
    if time_reason:
        reason_parts.append(str(time_reason))
    elif in_window is False:
        reason_parts.append("记录时间不在目标时间窗")
    if value_reason:
        reason_parts.append(str(value_reason))
    if status_reason:
        reason_parts.append(str(status_reason))
    reason = "；".join(reason_parts) or parent.reason

    entity = _first_value(record, "项目", "医嘱项", "实体", "名称", "entity", "item_name")
    value = _first_value(record, "结果", "数值", "value", "result_value", default=None)
    unit = _first_value(record, "单位", "unit", "result_unit")
    abnormal_flag = _first_value(record, "异常标志", "abnormal_flag")
    reference_range = _first_value(record, "参考范围", "reference_range")
    has_detail = any(value not in (None, "") for value in (entity, event_time, value, abnormal_flag, reference_range))
    data_quality = DataQuality.COMPLETE if has_detail and status != EvidenceStatus.UNKNOWN else DataQuality.PARTIAL
    if status == EvidenceStatus.UNKNOWN and not has_detail:
        data_quality = DataQuality.MISSING

    metadata = dict(parent.metadata)
    metadata.update(
        {
            key: value
            for key, value in {
                "in_time_window": in_window,
                "value_satisfied": value_satisfied,
                "status_satisfied": status_satisfied,
                "time_window": _first_value(record, "时间窗", "time_window"),
                "ordered_at": _first_value(record, "开立时间", "ordered_at"),
                "administered_at": _first_value(record, "执行/给药时间", "administered_at"),
                "evidence_time_role": _first_value(record, "证据时间角色", "evidence_time_role"),
                "predicate": _first_value(record, "证据谓词", "predicate"),
                "medication_status": _first_value(record, "医嘱状态", "medication_status"),
                "dose": _first_value(record, "剂量", "dose"),
                "dose_unit": _first_value(record, "剂量单位", "dose_unit"),
                "frequency": _first_value(record, "频次", "frequency"),
                "route": _first_value(record, "途径", "route"),
            }.items()
            if value is not None and value != ""
        }
    )
    return EvidenceItem(
        condition_id=condition_id,
        source_type=parent.source_type,
        source_name=parent.source_name,
        record_id=str(_first_value(record, "记录ID", "处方号", "record_id", "记录", default=parent.record_id)),
        document=parent.document,
        section=parent.section,
        entity=str(entity or parent.entity),
        raw_text=json.dumps(record, ensure_ascii=False, default=str),
        event_time=event_time,
        value=value,
        unit=str(unit or ""),
        abnormal_flag=str(abnormal_flag or ""),
        reference_range=str(reference_range or ""),
        status=status,
        reason_code=reason_code,
        data_quality=data_quality,
        reason=sanitize_user_text(reason),
        metadata=metadata,
    )


def build_condition_result(condition_result: dict[str, Any], condition_id: str) -> ConditionResult:
    evidence = []
    for item in condition_result.get("files", []) or []:
        if not isinstance(item, dict):
            continue
        candidate_records = item.get("候选记录") or item.get("candidate_records") or []
        structured_records = [record for record in candidate_records if isinstance(record, dict)]
        if structured_records:
            evidence.extend(
                _candidate_record_evidence(item, record, condition_id)
                for record in structured_records
            )
        elif item.get("_structured_evidence_records"):
            evidence.extend(
                _structured_record_evidence(item, record, condition_id)
                for record in item.get("_structured_evidence_records", [])
                if isinstance(record, dict)
            )
        else:
            evidence.append(adapt_legacy_evidence(item, condition_id))
    reason = sanitize_user_text(str(condition_result.get("reason", "") or ""))
    status = _evidence_status(condition_result)
    if not condition_result.get("status") and evidence:
        evidence_statuses = {item.status for item in evidence}
        if EvidenceStatus.MATCHED in evidence_statuses:
            status = EvidenceStatus.MATCHED
        elif EvidenceStatus.UNKNOWN in evidence_statuses:
            status = EvidenceStatus.UNKNOWN
        else:
            status = EvidenceStatus.NOT_MATCHED
    if status == EvidenceStatus.UNKNOWN:
        data_quality = (
            DataQuality.SOURCE_ERROR
            if any(item.data_quality == DataQuality.SOURCE_ERROR for item in evidence)
            else DataQuality.MISSING
        )
    elif evidence and all(item.data_quality == DataQuality.COMPLETE for item in evidence):
        data_quality = DataQuality.COMPLETE
    else:
        data_quality = DataQuality.PARTIAL
    return ConditionResult(
        condition_id=condition_id,
        condition=str(condition_result.get("condition", "") or ""),
        status=status,
        reason_code=_reason_code(status, reason, condition_result.get("reason_code")),
        reason=reason,
        data_quality=data_quality,
        evidence=evidence,
    )


def enrich_response_with_evidence_model(response: dict[str, Any], query_ir: Any = None) -> dict[str, Any]:
    """Add the v1 evidence contract while retaining every legacy response field."""
    if not isinstance(response, dict):
        return response
    ir_data = query_ir.to_dict() if hasattr(query_ir, "to_dict") else (query_ir or {})
    ir_conditions = ir_data.get("子条件", []) if isinstance(ir_data, dict) else []
    ids_by_text = {
        str(item.get("条件文本", "")): str(item.get("条件ID", ""))
        for item in ir_conditions
        if isinstance(item, dict) and item.get("条件文本")
    }
    for patient_result in response.get("results", []) or []:
        if not isinstance(patient_result, dict):
            continue
        normalized_conditions = []
        for index, (condition_text, item) in enumerate((patient_result.get("per_condition") or {}).items()):
            if not isinstance(item, dict):
                continue
            condition_id = ids_by_text.get(str(condition_text)) or f"c{index + 1}"
            unified = build_condition_result(item, condition_id)
            unified_dict = unified.to_dict()
            for source_result in item.get("files", []) or []:
                if isinstance(source_result, dict):
                    source_result.pop("_structured_evidence_records", None)
            item["evidence_items"] = unified_dict["evidence"]
            item["condition_result"] = unified_dict
            normalized_conditions.append(unified_dict)
        patient_result["condition_results"] = normalized_conditions
        patient_result["evidence_model_version"] = "1.0"
    response["evidence_model_version"] = "1.0"
    return response


def judgment_status(matched: bool, reason: str, per_condition: dict = None) -> tuple[str, bool]:
    texts = [str(reason or "")]
    has_supporting_file = False
    for item in (per_condition or {}).values():
        if isinstance(item, dict):
            texts.append(str(item.get("reason", "")))
            for f in item.get("files", []) or []:
                if isinstance(f, dict):
                    file_reason = str(f.get("reason", "") or "")
                    if f.get("matched") and not any(marker in file_reason for marker in UNKNOWN_MARKERS):
                        has_supporting_file = True
                    texts.append(str(f.get("reason", "")))
    joined = "；".join(texts)
    if matched and has_supporting_file:
        return "符合", True
    if any(marker in joined for marker in UNKNOWN_MARKERS):
        return "无法判断", False
    return ("符合" if matched else "不符合"), True


def _level(score: float) -> str:
    if score >= 0.8:
        return "高"
    if score >= 0.65:
        return "中"
    return "低"


def assess_file_confidence(file_result: dict[str, Any]) -> dict[str, Any]:
    """Estimate confidence from evidence shape, not from concrete query terms."""
    reason = sanitize_user_text(str(file_result.get("reason", "") or ""))
    fields = str(file_result.get("fields", "") or "")
    source = str(file_result.get("file", "") or "")
    matched = bool(file_result.get("matched", False))

    if any(marker in reason for marker in UNKNOWN_MARKERS):
        return {
            "置信度": 0.0,
            "置信等级": "无法判断",
            "依据等级": "数据源不可判定",
            "置信依据": "关键数据源失败或关键证据缺失",
        }

    text = " ".join([reason, fields, source])
    score = 0.62 if not matched else 0.68
    basis = "文本/候选证据"

    if any(token in text for token in ("结果=", "结果：", "异常判断=", "异常状态：", "找到检验项目但结果不符合", "未找到检验项目")):
        score = 0.9
        basis = "结构化检验规则"
    elif any(token in text for token in ("病史年限=", "原文明确否认")):
        score = 0.86
        basis = "结构化病史规则"
    elif any(token in text for token in ("时间窗口内", "与参考时间差", "当天找到", "之后", "之前")):
        score = 0.82
        basis = "结构化时间窗口"
    elif any(token in source for token in ("用药医嘱查询", "诊断查询", "就诊信息查询", "检验指标查询")):
        score = 0.78
        basis = "结构化接口字段"
    elif any(token in reason for token in ("关键字", "结构化字段")):
        score = 0.72
        basis = "结构化字段预筛"

    if "字段映射降级" in text or "未映射字段" in text:
        score = min(score, 0.66)
        basis += "，字段映射降级"
    if file_result.get("cot_response"):
        score = min(score, 0.68)
        basis += "，LLM参与判断"
    if "无相关字段" in reason or "无匹配字段" in fields:
        score = min(score, 0.58)
        basis = "证据字段不足"

    score = round(max(0.0, min(0.99, score)), 2)
    return {
        "置信度": score,
        "置信等级": _level(score),
        "依据等级": basis,
        "置信依据": sanitize_user_text(reason[:120] or basis),
    }


def assess_condition_confidence(condition_result: dict[str, Any]) -> dict[str, Any]:
    files = condition_result.get("files", []) or []
    matched = bool(condition_result.get("matched", False))
    reason = sanitize_user_text(str(condition_result.get("reason", "") or ""))
    status, conclusive = judgment_status(matched, reason, {condition_result.get("condition", ""): condition_result})
    if not conclusive:
        return {
            "判断状态": status,
            "可判定": False,
            "置信度": 0.0,
            "置信等级": "无法判断",
            "依据等级": "数据源不可判定",
        }

    scored_files = [assess_file_confidence(f) for f in files if isinstance(f, dict)]
    numeric_scores = [s["置信度"] for s in scored_files if s.get("置信等级") != "无法判断"]
    if numeric_scores:
        score = max(numeric_scores) if matched else max(numeric_scores)
    else:
        score = 0.6 if reason and reason != "无匹配" else 0.5
    score = round(score, 2)
    return {
        "判断状态": status,
        "可判定": True,
        "置信度": score,
        "置信等级": _level(score),
        "依据等级": "；".join(dict.fromkeys(s.get("依据等级", "") for s in scored_files if s.get("依据等级")))[:120],
    }


def assess_patient_confidence(matched: bool, reason: str, per_condition: dict = None) -> dict[str, Any]:
    reason = sanitize_user_text(str(reason or ""))
    status, conclusive = judgment_status(matched, reason, per_condition)
    if not conclusive:
        return {"判断状态": status, "可判定": False, "置信度": 0.0, "置信等级": "无法判断", "依据等级": "数据源不可判定"}

    condition_scores = []
    for item in (per_condition or {}).values():
        if isinstance(item, dict):
            assessed = item.get("置信评估") or assess_condition_confidence(item)
            if assessed.get("置信等级") != "无法判断":
                condition_scores.append(float(assessed.get("置信度", 0.0)))
    if condition_scores:
        score = min(condition_scores) if matched else max(condition_scores)
    else:
        score = 0.62
    score = round(max(0.0, min(0.99, score)), 2)
    return {"判断状态": status, "可判定": True, "置信度": score, "置信等级": _level(score), "依据等级": "条件证据综合"}
