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
from microharness.medical.evidence_policy import (
    adjudicate_source_decisions,
    resolve_source_role_policy,
)
from microharness.medical.record_identity import identity_from_binding, record_identity_config


class EvidenceStatus(str, Enum):
    NOT_MENTIONED = 'NOT_MENTIONED'
    MATCHED = "MATCHED"
    NOT_MATCHED = "NOT_MATCHED"
    UNKNOWN = "UNKNOWN"


class DataQuality(str, Enum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    MISSING = "MISSING"
    SOURCE_ERROR = "SOURCE_ERROR"


class EvidenceUncertaintyKind(str, Enum):
    """Machine-readable reason why an evidence source is inconclusive."""

    NONE = "NONE"
    SOURCE_FAILURE = "SOURCE_FAILURE"
    MISSING_CAPABILITY = "MISSING_CAPABILITY"
    TEMPORAL_UNRESOLVED = "TEMPORAL_UNRESOLVED"
    INCOMPLETE_SEARCH = "INCOMPLETE_SEARCH"
    REJECTED_CANDIDATE = "REJECTED_CANDIDATE"
    UNRESOLVED_CANDIDATE = "UNRESOLVED_CANDIDATE"


class EvidenceRole(str, Enum):
    PRIMARY = 'PRIMARY'
    SUPPORTING = 'SUPPORTING'
    CONTEXT = 'CONTEXT'
    TIME_ANCHOR = 'TIME_ANCHOR'
    CANDIDATE = 'CANDIDATE'


class ConflictLevel(str, Enum):
    NONE = 'NONE'
    SUPPORTING_DISAGREEMENT = 'SUPPORTING_DISAGREEMENT'
    CONCLUSIVE_CONFLICT = 'CONCLUSIVE_CONFLICT'


class ReasonCode(str, Enum):
    EVIDENCE_CONFLICT = 'EVIDENCE_CONFLICT'
    ENCOUNTER_CONTEXT_CONFLICT = 'ENCOUNTER_CONTEXT_CONFLICT'
    ENCOUNTER_IDENTITY_INSUFFICIENT = 'ENCOUNTER_IDENTITY_INSUFFICIENT'
    MATCH_CONFIRMED = "MATCH_CONFIRMED"
    NO_MATCHING_RECORD = "NO_MATCHING_RECORD"
    TIME_OUTSIDE_WINDOW = "TIME_OUTSIDE_WINDOW"
    MISSING_EVENT_TIME = "MISSING_EVENT_TIME"
    VALUE_CONDITION_NOT_MET = "VALUE_CONDITION_NOT_MET"
    STATUS_CONDITION_NOT_MET = "STATUS_CONDITION_NOT_MET"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    MISSING_REQUIRED_CAPABILITY = "MISSING_REQUIRED_CAPABILITY"
    SOURCE_ROLE_NOT_DECISIVE = "SOURCE_ROLE_NOT_DECISIVE"
    INCOMPLETE_CANDIDATE_SET = "INCOMPLETE_CANDIDATE_SET"
    QUANTIFIER_UNSUPPORTED = "QUANTIFIER_UNSUPPORTED"
    QUANTIFIER_SOURCE_UNKNOWN = "QUANTIFIER_SOURCE_UNKNOWN"
    QUANTIFIER_ANY_MATCHED = "QUANTIFIER_ANY_MATCHED"
    QUANTIFIER_ANY_NOT_MET = "QUANTIFIER_ANY_NOT_MET"
    QUANTIFIER_ANY_INDETERMINATE = "QUANTIFIER_ANY_INDETERMINATE"
    QUANTIFIER_ALL_MATCHED = "QUANTIFIER_ALL_MATCHED"
    QUANTIFIER_ALL_NOT_MET = "QUANTIFIER_ALL_NOT_MET"
    QUANTIFIER_ALL_EMPTY_SCOPE = "QUANTIFIER_ALL_EMPTY_SCOPE"
    QUANTIFIER_ALL_INDETERMINATE = "QUANTIFIER_ALL_INDETERMINATE"
    QUANTIFIER_SELECTION_EMPTY_SCOPE = "QUANTIFIER_SELECTION_EMPTY_SCOPE"
    QUANTIFIER_SELECTION_INDETERMINATE = "QUANTIFIER_SELECTION_INDETERMINATE"
    QUANTIFIER_RECORD_TIME_MISSING = "QUANTIFIER_RECORD_TIME_MISSING"
    QUANTIFIER_SELECTED_RECORD_MATCHED = "QUANTIFIER_SELECTED_RECORD_MATCHED"
    QUANTIFIER_SELECTED_RECORD_NOT_MET = "QUANTIFIER_SELECTED_RECORD_NOT_MET"
    QUANTIFIER_SELECTED_RECORD_UNKNOWN = "QUANTIFIER_SELECTED_RECORD_UNKNOWN"
    QUANTIFIER_COUNT_MISSING = "QUANTIFIER_COUNT_MISSING"
    QUANTIFIER_COUNT_MATCHED = "QUANTIFIER_COUNT_MATCHED"
    QUANTIFIER_COUNT_NOT_MET = "QUANTIFIER_COUNT_NOT_MET"
    QUANTIFIER_COUNT_INDETERMINATE = "QUANTIFIER_COUNT_INDETERMINATE"


_REJECTED_SEMANTIC_CANDIDATE_CODES = {
    "SEMANTIC_RECALL_EVIDENCE_MISSING",
    "SEMANTIC_RECALL_NON_VERBATIM_EVIDENCE",
    "SEMANTIC_RECALL_ENTITY_OUTSIDE_EVIDENCE",
    "SEMANTIC_RECALL_EXACT_MISMATCH",
}


def infer_evidence_uncertainty_kind(
    *,
    status: Any,
    reason_code: Any = "",
    data_quality: Any = "",
    missing_capabilities: Any = (),
    selection_complete: Any = None,
    semantic_trace: Any = (),
    explicit: Any = "",
) -> EvidenceUncertaintyKind:
    """Normalize executor diagnostics into the cross-source uncertainty contract."""
    status_text = status.value if isinstance(status, EvidenceStatus) else str(status or "").upper()
    if status_text != EvidenceStatus.UNKNOWN.value:
        return EvidenceUncertaintyKind.NONE

    capabilities = {
        str(value.value if hasattr(value, "value") else value).strip().upper()
        for value in (missing_capabilities or ())
    }
    if "TEMPORAL_OCCURRENCE" in capabilities:
        return EvidenceUncertaintyKind.TEMPORAL_UNRESOLVED
    if capabilities:
        return EvidenceUncertaintyKind.MISSING_CAPABILITY

    quality_text = data_quality.value if isinstance(data_quality, DataQuality) else str(data_quality or "").upper()
    code_text = str(reason_code or "").strip().upper()
    if quality_text == DataQuality.SOURCE_ERROR.value or code_text == ReasonCode.SOURCE_UNAVAILABLE.value:
        return EvidenceUncertaintyKind.SOURCE_FAILURE
    if selection_complete is False:
        return EvidenceUncertaintyKind.INCOMPLETE_SEARCH

    explicit_text = str(explicit or "").strip().upper()
    if explicit_text in EvidenceUncertaintyKind._value2member_map_:
        return EvidenceUncertaintyKind(explicit_text)

    trace_reasons = set()
    for item in (semantic_trace or ()):
        if not isinstance(item, dict) or not (
            item.get("accepted") is False
            or str(item.get("status") or "").strip().upper() == EvidenceStatus.UNKNOWN.value
        ):
            continue
        trace_reasons.update(
            str(item.get(key) or "").strip().upper()
            for key in ("reason_code", "reason")
            if str(item.get(key) or "").strip()
        )
    if code_text in _REJECTED_SEMANTIC_CANDIDATE_CODES or (
        trace_reasons & _REJECTED_SEMANTIC_CANDIDATE_CODES
    ):
        return EvidenceUncertaintyKind.REJECTED_CANDIDATE
    if code_text == ReasonCode.MISSING_EVENT_TIME.value:
        return EvidenceUncertaintyKind.TEMPORAL_UNRESOLVED
    return EvidenceUncertaintyKind.UNRESOLVED_CANDIDATE


@dataclass
class EvidenceItem:
    """Canonical machine-readable evidence emitted by every source adapter."""

    condition_id: str
    source_type: str
    source_name: str
    source_role: EvidenceRole = EvidenceRole.CANDIDATE
    record_id: str = ""
    record_id_label: str = ""
    record_id_field: str = ""
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
        data['source_role'] = self.source_role.value
        data["status"] = self.status.value
        data["reason_code"] = self.reason_code.value
        data["data_quality"] = self.data_quality.value
        return data


@dataclass
class ConditionResult:
    """Canonical four-state result for one atomic condition."""

    condition_id: str
    condition: str
    status: EvidenceStatus
    reason_code: ReasonCode
    reason: str
    data_quality: DataQuality
    evidence: list[EvidenceItem] = field(default_factory=list)
    conflict_level: ConflictLevel = ConflictLevel.NONE
    source_decisions: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            'conflict_level': self.conflict_level.value,
            'source_decisions': list(self.source_decisions),
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


@dataclass(frozen=True)
class ConditionAdjudicationRequest:
    """Typed input for condition-level cross-source adjudication."""

    condition_id: str
    condition: str
    evidence: tuple[EvidenceItem, ...] = ()
    original_status: EvidenceStatus = EvidenceStatus.UNKNOWN
    original_reason: str = ""
    original_reason_code: str = ""


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
        quantifier_mode = str(file_result.get("quantifier_mode") or "").strip()
        if quantifier_mode:
            item["量词裁决"] = {
                "模式": quantifier_mode,
                "目标次数": file_result.get("quantifier_count"),
                "单位": str(file_result.get("quantifier_unit") or ""),
                "记录统计": dict(file_result.get("record_status_counts") or {}),
                "候选完整": bool(file_result.get("selection_complete", True)),
                "选中记录": list(file_result.get("量词选中记录") or []),
            }
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

NOT_MENTIONED_MARKERS = (
    '\u672a\u627e\u5230\u4e0e',
    '\u672a\u627e\u5230\u68c0\u9a8c\u9879\u76ee',
    '\u672a\u627e\u5230\u53ef\u5224\u5b9a\u7684\u68c0\u9a8c\u9879\u76ee',
    '\u672a\u5728\u7ed3\u6784\u5316\u5b57\u6bb5\u4e2d\u51fa\u73b0',
    '\u5173\u952e\u5b57\u672a\u5728\u6570\u636e\u4e2d\u51fa\u73b0',
    '\u672a\u627e\u5230\u5339\u914d',
    '\u65e0\u5339\u914d\u8bb0\u5f55',
    '\u65e0\u5339\u914d',
)


def status_label(status: EvidenceStatus | str) -> str:
    try:
        normalized = status if isinstance(status, EvidenceStatus) else EvidenceStatus(str(status).upper())
    except (TypeError, ValueError):
        normalized = EvidenceStatus.UNKNOWN
    return {
        EvidenceStatus.MATCHED: '\u7b26\u5408',
        EvidenceStatus.NOT_MATCHED: '\u4e0d\u7b26\u5408',
        EvidenceStatus.NOT_MENTIONED: '\u672a\u63d0\u53ca',
        EvidenceStatus.UNKNOWN: '\u65e0\u6cd5\u5224\u65ad',
    }[normalized]


def combine_condition_statuses(
    statuses: list[EvidenceStatus | str],
    *,
    use_and: bool,
) -> EvidenceStatus:
    '''Combine atomic condition statuses without collapsing them to booleans.'''
    normalized: list[EvidenceStatus] = []
    aliases = {
        '\u7b26\u5408': EvidenceStatus.MATCHED,
        '\u4e0d\u7b26\u5408': EvidenceStatus.NOT_MATCHED,
        '\u672a\u63d0\u53ca': EvidenceStatus.NOT_MENTIONED,
        '\u65e0\u6cd5\u5224\u65ad': EvidenceStatus.UNKNOWN,
    }
    for value in statuses:
        if isinstance(value, EvidenceStatus):
            normalized.append(value)
            continue
        text = str(value or '').upper()
        if text in EvidenceStatus._value2member_map_:
            normalized.append(EvidenceStatus(text))
        elif str(value or '') in aliases:
            normalized.append(aliases[str(value or '')])
        else:
            normalized.append(EvidenceStatus.UNKNOWN)
    if not normalized:
        return EvidenceStatus.UNKNOWN
    present = set(normalized)
    if use_and:
        if EvidenceStatus.NOT_MATCHED in present:
            return EvidenceStatus.NOT_MATCHED
        if EvidenceStatus.UNKNOWN in present:
            return EvidenceStatus.UNKNOWN
        if EvidenceStatus.NOT_MENTIONED in present:
            return EvidenceStatus.NOT_MENTIONED
        return EvidenceStatus.MATCHED
    if EvidenceStatus.MATCHED in present:
        return EvidenceStatus.MATCHED
    if EvidenceStatus.UNKNOWN in present:
        return EvidenceStatus.UNKNOWN
    if EvidenceStatus.NOT_MENTIONED in present:
        return EvidenceStatus.NOT_MENTIONED
    return EvidenceStatus.NOT_MATCHED


def _overall_reason_code(status: EvidenceStatus, *, use_and: bool, condition_count: int) -> str:
    if condition_count <= 1:
        return "SINGLE_CONDITION_RESULT"
    if status == EvidenceStatus.MATCHED:
        return "ALL_CONDITIONS_MATCHED" if use_and else "ANY_CONDITION_MATCHED"
    if status == EvidenceStatus.NOT_MATCHED:
        return "CONDITION_NOT_MATCHED" if use_and else "ALL_CONDITIONS_NOT_MATCHED"
    if status == EvidenceStatus.NOT_MENTIONED:
        return "CONDITION_NOT_MENTIONED"
    return "CONDITION_UNKNOWN"


def _overall_reason(
    conditions: list[dict[str, Any]],
    status: EvidenceStatus,
    *,
    use_and: bool,
) -> str:
    if not conditions:
        return "没有可用于总体裁决的子条件结果。"
    details = []
    for index, condition in enumerate(conditions, 1):
        label = status_label(str(condition.get("status") or "UNKNOWN"))
        text = str(condition.get("condition") or condition.get("condition_id") or f"条件{index}")
        details.append(f"条件{index}「{text}」为{label}")
    if len(conditions) == 1:
        prefix = "单条件裁决"
    elif use_and:
        prefix = "AND（全部满足）组合"
    else:
        prefix = "OR（任一满足）组合"
    return f"{prefix}结果为{status_label(status)}：" + "；".join(details) + "。"


def build_overall_result(
    condition_results: list[dict[str, Any]],
    *,
    connector: Any = None,
) -> dict[str, Any]:
    """Build the canonical compound result from adjudicated atomic conditions."""
    normalized_connector = str(connector or "").strip().lower()
    use_and = normalized_connector != "or"
    statuses = [str(item.get("status") or "UNKNOWN") for item in condition_results]
    status = combine_condition_statuses(statuses, use_and=use_and)
    public_connector = "SINGLE" if len(condition_results) <= 1 else ("AND" if use_and else "OR")
    conditions = [
        {
            "condition_id": str(item.get("condition_id") or f"c{index + 1}"),
            "condition": str(item.get("condition") or ""),
            "status": str(item.get("status") or EvidenceStatus.UNKNOWN.value),
            "reason_code": str(item.get("reason_code") or ""),
        }
        for index, item in enumerate(condition_results)
        if isinstance(item, dict)
    ]
    return {
        "connector": public_connector,
        "status": status.value,
        "matched": status == EvidenceStatus.MATCHED,
        "conclusive": status != EvidenceStatus.UNKNOWN,
        "判断状态": status_label(status),
        "reason_code": _overall_reason_code(
            status,
            use_and=use_and,
            condition_count=len(conditions),
        ),
        "reason": _overall_reason(conditions, status, use_and=use_and),
        "conditions": conditions,
    }


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


_ROLE_ALIASES = {
    'PRIMARY': EvidenceRole.PRIMARY,
    '\u4e3b\u8bc1\u636e': EvidenceRole.PRIMARY,
    'SUPPORTING': EvidenceRole.SUPPORTING,
    '\u8f85\u52a9\u8bc1\u636e': EvidenceRole.SUPPORTING,
    '\u8f85\u52a9\u4f9d\u636e': EvidenceRole.SUPPORTING,
    'CONTEXT': EvidenceRole.CONTEXT,
    '\u4e0a\u4e0b\u6587': EvidenceRole.CONTEXT,
    'TIME_ANCHOR': EvidenceRole.TIME_ANCHOR,
    '\u65f6\u95f4\u8303\u56f4\u4f9d\u636e': EvidenceRole.TIME_ANCHOR,
    '\u4e8b\u4ef6\u65f6\u95f4\u951a\u70b9': EvidenceRole.TIME_ANCHOR,
    'CANDIDATE': EvidenceRole.CANDIDATE,
    '\u5019\u9009\u8bc1\u636e': EvidenceRole.CANDIDATE,
}


def _evidence_role(value: Any) -> EvidenceRole:
    text = str(value or '').strip()
    return _ROLE_ALIASES.get(text, _ROLE_ALIASES.get(text.upper(), EvidenceRole.CANDIDATE))


def _optional_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None or value == '':
        return None
    text = str(value).strip().lower()
    if text in {'true', '1', 'yes', 'y', '是'}:
        return True
    if text in {'false', '0', 'no', 'n', '否'}:
        return False
    return None


def _selected_field_labels(file_result: dict[str, Any]) -> list[str]:
    raw_fields = str(_first_value(file_result, 'fields', 'raw_text', '原文') or '')
    labels = []
    for part in re.split(r'[\r\n|]+', raw_fields):
        text = part.strip()
        if not text or text.startswith('('):
            continue
        label = re.split(r'[:：]', text, maxsplit=1)[0].strip()
        label = re.sub(r'^\[[^\]]+\]\s*', '', label).strip()
        if label:
            labels.append(label)
    return labels


def _section_name_matches(value: str, section: Any) -> bool:
    left = re.sub(r'[\s:：_\-/]+', '', str(value or '')).lower()
    right = re.sub(r'[\s:：_\-/]+', '', str(section or '')).lower()
    return bool(left and right and (left in right or right in left))


def _uses_only_anchor_sections(file_result: dict[str, Any], anchor_sections: Any) -> bool:
    labels = _selected_field_labels(file_result)
    sections = [str(section).strip() for section in (anchor_sections or ()) if str(section).strip()]
    return bool(labels and sections) and all(
        any(_section_name_matches(label, section) for section in sections)
        for label in labels
    )


def annotate_evidence_source(file_result: dict[str, Any], source: dict[str, Any], **options) -> dict[str, Any]:
    if not isinstance(file_result, dict) or not isinstance(source, dict):
        return file_result
    service_id = str(source.get('service_id') or file_result.get('service_id') or '').strip()
    semantic = source.get('semantic') if isinstance(source.get('semantic'), dict) else {}
    entity_type = str(semantic.get('entity_type') or '').strip().lower()
    domain = str(semantic.get('domain') or source.get('domain') or '').strip().lower()
    evidence_types = semantic.get('evidence_types') or source.get('evidence_types') or []
    if not isinstance(evidence_types, (list, tuple, set)):
        evidence_types = [evidence_types]
    evidence_types = list(dict.fromkeys(
        str(value).strip() for value in evidence_types if str(value).strip()
    ))
    presentation = semantic.get('presentation') if isinstance(semantic.get('presentation'), dict) else {}
    record_identity = record_identity_config(semantic)
    if record_identity['label'] and record_identity['fields']:
        file_result['record_id_label'] = record_identity['label']
        file_result['record_id_fields'] = list(record_identity['fields'])
    record_type = str(
        presentation.get('record_type')
        or semantic.get('record_type')
        or entity_type
        or domain
        or ('service' if service_id else 'document')
    ).strip().lower()
    source_kind = 'service' if service_id else 'document'
    # Keep source_type backward compatible while exposing a stable, generic
    # source contract for clients and future skills.
    file_result['source_type'] = entity_type if entity_type in {'diagnosis', 'encounter'} else source_kind
    file_result['source_kind'] = source_kind
    if domain:
        file_result['domain'] = domain
    if entity_type:
        file_result['entity_type'] = entity_type
    if evidence_types:
        file_result['evidence_types'] = evidence_types
        file_result['evidence_type'] = evidence_types[0]
    if record_type:
        file_result['record_type'] = record_type
    source_name = str(source.get('file') or file_result.get('file') or '').strip()
    template = str(source.get('template') or file_result.get('template') or '').strip()
    source_label = str(
        source.get('label')
        or semantic.get('label')
        or file_result.get('source_label')
        or source_name
    ).strip()
    if source_label:
        file_result['source_label'] = source_label
    primary_source_id = str(options.get('primary_source_id') or '')
    time_source_id = str(options.get('time_source_id') or '').split('.', 1)[0]
    routed_documents = options.get('routed_documents') or ()
    anchor_documents = options.get('anchor_documents') or ()
    anchor_sections = options.get('anchor_sections') or ()
    is_anchor = any(
        name and (source_name.startswith(str(name)) or template == str(name))
        for name in anchor_documents
    )
    is_routed = any(
        name and (source_name.startswith(str(name)) or template == str(name))
        for name in routed_documents
    )
    anchor_only = is_anchor and _uses_only_anchor_sections(file_result, anchor_sections)
    policy_raw = dict(file_result)
    policy_raw.update(source)
    role_policy = resolve_source_role_policy(
        raw=policy_raw,
        semantic=semantic,
        semantic_type=str(
            file_result.get('semantic_type') or semantic.get('semantic_type') or ''
        ).strip().upper(),
        source_kind=source_kind,
        is_primary_source=bool(service_id and service_id == primary_source_id),
        has_primary_source=bool(primary_source_id),
        is_time_anchor=bool(
            (service_id and service_id == time_source_id)
            or (not service_id and is_anchor and (not is_routed or anchor_only))
        ),
        is_routed=bool(not service_id and is_routed),
        acceptable_roles=(
            file_result.get('acceptable_source_roles')
            or semantic.get('acceptable_source_roles')
            or ()
        ),
        missing_capabilities=file_result.get('missing_capabilities') or (),
    )
    role = _evidence_role(role_policy.role)
    file_result['source_role'] = role.value
    file_result['evidence_role'] = role.value
    file_result['source_role_policy_source'] = role_policy.policy_source
    file_result['source_role_decisive'] = role_policy.decisive
    purpose = _first_value(source, 'purpose', 'description')
    if not purpose:
        purpose = _first_value(semantic, 'purpose', 'description')
    if purpose:
        file_result.setdefault('purpose', str(purpose))
        file_result.setdefault('\u7528\u9014', str(purpose))
    if service_id:
        file_result['service_id'] = service_id
    logical_source_id = str(
        source.get('logical_source_id')
        or source.get('evidence_source_id')
        or source.get('source_id')
        or file_result.get('logical_source_id')
        or ''
    ).strip()
    if not logical_source_id and service_id:
        logical_source_id = f'service:{service_id}'
    if not logical_source_id and template:
        logical_source_id = f'document:{template}'
    if logical_source_id:
        file_result['logical_source_id'] = logical_source_id
    if 'time_window_required' in options:
        file_result['time_window_required'] = bool(options.get('time_window_required'))
    if 'time_window_resolved' in options:
        file_result['time_window_resolved'] = bool(options.get('time_window_resolved'))
    file_result.setdefault('source_name', source_name)
    file_result.setdefault('template', template)
    return file_result


def _evidence_status(file_result: dict[str, Any]) -> EvidenceStatus:
    explicit = str(_first_value(file_result, "status", "判断状态")).upper()
    status_map = {
        'NOT_MENTIONED': EvidenceStatus.NOT_MENTIONED,
        '\u672a\u63d0\u53ca': EvidenceStatus.NOT_MENTIONED,
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
    if any(marker in reason for marker in NOT_MENTIONED_MARKERS):
        return EvidenceStatus.NOT_MENTIONED
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
    if status == EvidenceStatus.NOT_MENTIONED:
        return ReasonCode.NO_MATCHING_RECORD
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
        for key in (
            "evidence_role", "证据角色", "purpose", "用途",
            "logical_source_id", "source_id", "evidence_source_id",
            "source_kind", "source_label", "domain", "entity_type",
            "evidence_type", "evidence_types", "record_type",
            "executor", "semantic_type",
            "supported_capabilities", "required_capabilities", "missing_capabilities",
            "acceptable_source_roles", "source_role_acceptable",
            "source_role_policy_source", "source_role_decisive",
            "selection_complete", "candidate_records_complete",
            "time_window_required", "time_window_resolved",
            "uncertainty_kind",
        )
        if file_result.get(key) not in (None, "")
    }
    if isinstance(file_result.get("semantic_trace"), list):
        metadata["semantic_trace"] = file_result["semantic_trace"]
    source_reason_code = str(file_result.get("reason_code") or "").strip().upper()
    if source_reason_code:
        metadata["source_reason_code"] = source_reason_code
    metadata["uncertainty_kind"] = infer_evidence_uncertainty_kind(
        status=status,
        reason_code=source_reason_code,
        data_quality=file_result.get("data_quality"),
        missing_capabilities=file_result.get("missing_capabilities"),
        selection_complete=file_result.get(
            "selection_complete",
            file_result.get("candidate_records_complete"),
        ),
        semantic_trace=file_result.get("semantic_trace"),
        explicit=file_result.get("uncertainty_kind"),
    ).value
    quantifier_mode = str(file_result.get("quantifier_mode") or "").strip()
    if quantifier_mode:
        metadata.update({
            "quantifier_adjudicated": True,
            "quantifier_mode": quantifier_mode,
            "quantifier_count": file_result.get("quantifier_count"),
            "quantifier_unit": str(file_result.get("quantifier_unit") or ""),
            "record_status_counts": dict(file_result.get("record_status_counts") or {}),
            "selection_complete": bool(file_result.get("selection_complete", True)),
            "selected_candidate_records": list(file_result.get("量词选中记录") or []),
        })
    source_role = _evidence_role(
        _first_value(file_result, 'source_role', 'evidence_role', '\u8bc1\u636e\u89d2\u8272')
    )
    service_id = str(file_result.get('service_id') or '').strip()
    if service_id:
        metadata['service_id'] = service_id
    metadata['source_role'] = source_role.value
    return EvidenceItem(
        condition_id=condition_id,
        source_type=source_type,
        source_role=source_role,
        source_name=str(_first_value(file_result, "source_name", "file", "service", "document")),
        record_id=str(_first_value(file_result, "record_id", "recordId", "id")),
        record_id_label=str(_first_value(file_result, "record_id_label", "记录标识名称")),
        record_id_field=str(_first_value(file_result, "record_id_field", "记录标识字段")),
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
        identities = {}
        for binding in bindings:
            record_id, field_name, value, path = _binding_parts(binding)
            if value:
                group_id = record_id or "记录1"
                grouped.setdefault(group_id, []).append((field_name, value, path))
                identity = identity_from_binding(binding)
                if identity:
                    identities.setdefault(group_id, identity)
        for internal_id, parts in grouped.items():
            fields = {name: value for name, value, _ in parts}
            identity = identities.get(internal_id, {})
            entity = next((v for k, v in fields.items() if k.endswith("名称")), "")
            if not entity:
                entity = next((fields.get(k) for k in ("就诊类型", "就诊科室", "就诊状态", "当前病区") if fields.get(k)), options.get("entity", ""))
            records.append(common | {
                "record_id": identity.get("record_id") or internal_id,
                "record_id_label": identity.get("record_id_label") or "",
                "record_id_field": identity.get("record_id_field") or "",
                "entity": entity,
                "raw_text": json.dumps(fields, ensure_ascii=False),
                "event_time": _record_event_time(fields), "data_quality": "COMPLETE",
                "metadata": {"service_id": source.get("service_id", ""),
                    "semantic": source.get("semantic", {}), "condition": options.get("condition", ""),
                    "internal_record_id": internal_id, "fields": fields,
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
    if metadata.get("quantifier_adjudicated"):
        metadata["quantifier_candidate_record"] = True
    metadata.update(record.get("metadata") if isinstance(record.get("metadata"), dict) else {})
    return EvidenceItem(condition_id=condition_id,
        source_role=parent.source_role,
        source_type=str(record.get("source_type") or parent.source_type),
        source_name=str(record.get("source_name") or parent.source_name),
        record_id=str(record.get("record_id") or parent.record_id),
        record_id_label=str(record.get("record_id_label") or parent.record_id_label),
        record_id_field=str(record.get("record_id_field") or parent.record_id_field),
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
    in_window = _optional_bool(in_window)
    value_satisfied = _optional_bool(value_satisfied)
    status_satisfied = _optional_bool(status_satisfied)
    time_window_required = bool(_optional_bool(_first_value(
        record,
        '时间窗必需',
        'time_window_required',
        default=_first_value(file_result, 'time_window_required', default=False),
    )))
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
    elif time_window_required and in_window is None:
        status = EvidenceStatus.UNKNOWN
        reason_code = ReasonCode.MISSING_EVENT_TIME
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
    elif time_window_required and in_window is None:
        reason_parts.append('目标时间窗未解析，无法判断记录是否在时间窗内')
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
    if metadata.get("quantifier_adjudicated"):
        metadata["quantifier_candidate_record"] = True
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
    business_record_id = str(_first_value(record, "记录ID", "record_id", default=parent.record_id) or "")
    record_id_label = str(_first_value(record, "记录标识名称", "record_id_label", default=parent.record_id_label) or "")
    record_id_field = str(_first_value(record, "记录标识字段", "record_id_field", default=parent.record_id_field) or "")
    internal_record_id = str(_first_value(record, "记录序号", "记录", default="") or "")
    if internal_record_id:
        metadata["internal_record_id"] = internal_record_id
    if not business_record_id:
        business_record_id = internal_record_id
        record_id_label = ""
        record_id_field = ""
    return EvidenceItem(
        condition_id=condition_id,
        source_type=parent.source_type,
        source_role=parent.source_role,
        source_name=parent.source_name,
        record_id=business_record_id,
        record_id_label=record_id_label,
        record_id_field=record_id_field,
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


def _collect_condition_evidence(
    condition_result: dict[str, Any],
    condition_id: str,
) -> list[EvidenceItem]:
    evidence = []
    for item in condition_result.get('files', []) or []:
        if not isinstance(item, dict):
            continue
        candidate_records = item.get('\u5019\u9009\u8bb0\u5f55') or item.get('candidate_records') or []
        structured_records = [record for record in candidate_records if isinstance(record, dict)]
        if structured_records:
            if str(item.get("quantifier_mode") or "").strip():
                evidence.append(adapt_legacy_evidence(item, condition_id))
            evidence.extend(
                _candidate_record_evidence(item, record, condition_id)
                for record in structured_records
            )
        elif item.get('_structured_evidence_records'):
            evidence.extend(
                _structured_record_evidence(item, record, condition_id)
                for record in item.get('_structured_evidence_records', [])
                if isinstance(record, dict)
            )
        else:
            evidence.append(adapt_legacy_evidence(item, condition_id))
    return evidence


def _source_identity(item: EvidenceItem) -> str:
    logical_source_id = str(
        item.metadata.get('logical_source_id')
        or item.metadata.get('evidence_source_id')
        or item.metadata.get('source_id')
        or ''
    ).strip()
    if logical_source_id:
        return logical_source_id
    service_id = str(item.metadata.get('service_id') or '').strip()
    if service_id:
        return f'service:{service_id}'
    template = str(item.metadata.get('template') or item.document or '').strip()
    source_name = str(item.source_name or '').strip()
    if template:
        return f'document:{template}'
    return f'{item.source_type or "source"}:{source_name}'


def _metadata_values(items: list[EvidenceItem], key: str) -> list[str]:
    values = []
    for item in items:
        raw = item.metadata.get(key)
        candidates = raw if isinstance(raw, (list, tuple, set)) else [raw]
        for candidate in candidates:
            text = str(candidate or '').strip()
            if text and text not in values:
                values.append(text)
    return values


def _source_selection_complete(items: list[EvidenceItem]) -> bool:
    flags = []
    for item in items:
        for key in ('selection_complete', 'candidate_records_complete'):
            if key in item.metadata:
                flags.append(bool(_optional_bool(item.metadata.get(key))))
    return all(flags) if flags else True


def _aggregate_data_quality(items: list[EvidenceItem]) -> DataQuality:
    qualities = {item.data_quality for item in items}
    if qualities == {DataQuality.COMPLETE}:
        return DataQuality.COMPLETE
    if qualities == {DataQuality.MISSING}:
        return DataQuality.MISSING
    if DataQuality.SOURCE_ERROR in qualities and not any(
        item.status != EvidenceStatus.UNKNOWN for item in items
    ):
        return DataQuality.SOURCE_ERROR
    return DataQuality.PARTIAL


def _aggregate_source_status(items: list[EvidenceItem]) -> EvidenceStatus:
    statuses = {item.status for item in items}
    if EvidenceStatus.MATCHED in statuses:
        return EvidenceStatus.MATCHED
    if EvidenceStatus.UNKNOWN in statuses:
        return EvidenceStatus.UNKNOWN
    if EvidenceStatus.NOT_MATCHED in statuses:
        return EvidenceStatus.NOT_MATCHED
    if EvidenceStatus.NOT_MENTIONED in statuses:
        return EvidenceStatus.NOT_MENTIONED
    return EvidenceStatus.UNKNOWN


def _aggregate_uncertainty_kind(
    items: list[EvidenceItem],
    status: EvidenceStatus,
) -> EvidenceUncertaintyKind:
    if status != EvidenceStatus.UNKNOWN:
        return EvidenceUncertaintyKind.NONE

    unknown_items = [item for item in items if item.status == EvidenceStatus.UNKNOWN]
    explicit = []
    for item in unknown_items:
        value = str(item.metadata.get("uncertainty_kind") or "").strip().upper()
        if value in EvidenceUncertaintyKind._value2member_map_:
            explicit.append(EvidenceUncertaintyKind(value))
    kinds = set(explicit)
    if EvidenceUncertaintyKind.TEMPORAL_UNRESOLVED in kinds:
        return EvidenceUncertaintyKind.TEMPORAL_UNRESOLVED
    if EvidenceUncertaintyKind.MISSING_CAPABILITY in kinds:
        return EvidenceUncertaintyKind.MISSING_CAPABILITY
    if EvidenceUncertaintyKind.INCOMPLETE_SEARCH in kinds:
        return EvidenceUncertaintyKind.INCOMPLETE_SEARCH
    if EvidenceUncertaintyKind.SOURCE_FAILURE in kinds:
        return EvidenceUncertaintyKind.SOURCE_FAILURE
    if EvidenceUncertaintyKind.UNRESOLVED_CANDIDATE in kinds:
        return EvidenceUncertaintyKind.UNRESOLVED_CANDIDATE
    if kinds == {EvidenceUncertaintyKind.REJECTED_CANDIDATE}:
        return EvidenceUncertaintyKind.REJECTED_CANDIDATE
    if any(item.reason_code == ReasonCode.MISSING_EVENT_TIME for item in unknown_items):
        return EvidenceUncertaintyKind.TEMPORAL_UNRESOLVED
    if any(item.reason_code == ReasonCode.SOURCE_UNAVAILABLE for item in unknown_items):
        return EvidenceUncertaintyKind.SOURCE_FAILURE
    return EvidenceUncertaintyKind.UNRESOLVED_CANDIDATE


def _decision_reason_code(items: list[EvidenceItem], status: EvidenceStatus) -> ReasonCode:
    relevant = [item.reason_code for item in items if item.status == status]
    if status == EvidenceStatus.MATCHED:
        return ReasonCode.MATCH_CONFIRMED
    if status == EvidenceStatus.UNKNOWN:
        if ReasonCode.SOURCE_UNAVAILABLE in relevant:
            return ReasonCode.SOURCE_UNAVAILABLE
        return relevant[0] if relevant else ReasonCode.INSUFFICIENT_EVIDENCE
    if status == EvidenceStatus.NOT_MENTIONED:
        return ReasonCode.NO_MATCHING_RECORD
    unique = list(dict.fromkeys(relevant))
    return unique[0] if len(unique) == 1 else ReasonCode.NO_MATCHING_RECORD


def _source_decisions(evidence: list[EvidenceItem]) -> list[dict[str, Any]]:
    grouped: dict[str, list[EvidenceItem]] = {}
    for item in evidence:
        grouped.setdefault(_source_identity(item), []).append(item)

    role_priority = {
        EvidenceRole.PRIMARY: 5,
        EvidenceRole.SUPPORTING: 4,
        EvidenceRole.CANDIDATE: 3,
        EvidenceRole.CONTEXT: 2,
        EvidenceRole.TIME_ANCHOR: 1,
    }
    decisions = []
    for source_id, items in grouped.items():
        adjudicated = [
            item for item in items
            if item.metadata.get("quantifier_adjudicated")
            and not item.metadata.get("quantifier_candidate_record")
        ]
        voting_items = adjudicated or items
        role = max(
            (item.source_role for item in voting_items),
            key=lambda value: role_priority[value],
        )
        status = _aggregate_source_status(voting_items)
        quality = _aggregate_data_quality(voting_items)
        reason_code = _decision_reason_code(voting_items, status)
        uncertainty_kind = _aggregate_uncertainty_kind(voting_items, status)
        supported_capabilities = _metadata_values(voting_items, 'supported_capabilities')
        required_capabilities = _metadata_values(voting_items, 'required_capabilities')
        missing_capabilities = _metadata_values(voting_items, 'missing_capabilities')
        source_role_acceptable_values = [
            _optional_bool(item.metadata.get('source_role_acceptable'))
            for item in voting_items
            if 'source_role_acceptable' in item.metadata
        ]
        source_role_acceptable = not any(value is False for value in source_role_acceptable_values)
        selection_complete = _source_selection_complete(voting_items)
        adjudication_note = ""
        if status == EvidenceStatus.MATCHED and missing_capabilities:
            status = EvidenceStatus.UNKNOWN
            reason_code = ReasonCode.MISSING_REQUIRED_CAPABILITY
            quality = DataQuality.PARTIAL
            uncertainty_kind = (
                EvidenceUncertaintyKind.TEMPORAL_UNRESOLVED
                if "TEMPORAL_OCCURRENCE" in missing_capabilities
                else EvidenceUncertaintyKind.MISSING_CAPABILITY
            )
            adjudication_note = (
                "当前来源缺少完成条件判断所需的证据能力："
                + "、".join(missing_capabilities)
            )
        elif status == EvidenceStatus.MATCHED and not source_role_acceptable:
            status = EvidenceStatus.UNKNOWN
            reason_code = ReasonCode.SOURCE_ROLE_NOT_DECISIVE
            quality = DataQuality.PARTIAL
            uncertainty_kind = EvidenceUncertaintyKind.MISSING_CAPABILITY
            adjudication_note = "当前来源角色只能提供上下文或时间锚点，不能独立证明条件"
        elif (
            not selection_complete
            and status == EvidenceStatus.NOT_MENTIONED
        ) or (
            not selection_complete
            and status == EvidenceStatus.NOT_MATCHED
            and reason_code == ReasonCode.NO_MATCHING_RECORD
        ):
            status = EvidenceStatus.UNKNOWN
            reason_code = ReasonCode.INCOMPLETE_CANDIDATE_SET
            quality = DataQuality.PARTIAL
            uncertainty_kind = EvidenceUncertaintyKind.INCOMPLETE_SEARCH
            adjudication_note = "当前来源的候选记录集合不完整，未检索到目标不能形成否定结论"
        reasons = list(dict.fromkeys(item.reason for item in voting_items if item.reason))
        if adjudication_note:
            reasons.append(adjudication_note)
        decisions.append({
            'source_id': source_id,
            'source_name': items[0].source_name,
            'source_type': items[0].source_type,
            'source_role': role.value,
            'status': status.value,
            'reason_code': reason_code.value,
            'data_quality': quality.value,
            'evidence_count': len(items),
            'record_ids': [item.record_id for item in items if item.record_id],
            'reason': '\uff1b'.join(reasons[:3]),
            'quantifier': dict(adjudicated[0].metadata) if adjudicated else None,
            'selection_complete': selection_complete,
            'supported_capabilities': supported_capabilities,
            'required_capabilities': required_capabilities,
            'missing_capabilities': missing_capabilities,
            'source_role_acceptable': source_role_acceptable,
            'uncertainty_kind': uncertainty_kind.value,
        })
    return decisions


def _resolve_decision_statuses_legacy(
    decisions: list[dict[str, Any]],
) -> tuple[EvidenceStatus, ConflictLevel, ReasonCode]:
    voting = [
        item for item in decisions
        if item.get('source_role') not in {
            EvidenceRole.CONTEXT.value,
            EvidenceRole.TIME_ANCHOR.value,
        }
    ]
    if not voting:
        return EvidenceStatus.UNKNOWN, ConflictLevel.NONE, ReasonCode.INSUFFICIENT_EVIDENCE

    primary = [item for item in voting if item.get('source_role') == EvidenceRole.PRIMARY.value]
    supporting = [
        item for item in voting
        if item.get('source_role') != EvidenceRole.PRIMARY.value
    ]
    if not primary:
        primary, supporting = supporting, []

    primary_statuses = {EvidenceStatus(item['status']) for item in primary}
    supporting_statuses = {EvidenceStatus(item['status']) for item in supporting}
    primary_has_match = EvidenceStatus.MATCHED in primary_statuses
    primary_has_negative = EvidenceStatus.NOT_MATCHED in primary_statuses
    primary_has_unknown = EvidenceStatus.UNKNOWN in primary_statuses

    if primary_has_match and primary_has_negative:
        return EvidenceStatus.UNKNOWN, ConflictLevel.CONCLUSIVE_CONFLICT, ReasonCode.EVIDENCE_CONFLICT
    if primary_has_match:
        if EvidenceStatus.NOT_MATCHED in supporting_statuses:
            return EvidenceStatus.MATCHED, ConflictLevel.SUPPORTING_DISAGREEMENT, ReasonCode.MATCH_CONFIRMED
        return EvidenceStatus.MATCHED, ConflictLevel.NONE, ReasonCode.MATCH_CONFIRMED
    if primary_has_negative:
        if EvidenceStatus.MATCHED in supporting_statuses:
            return EvidenceStatus.UNKNOWN, ConflictLevel.CONCLUSIVE_CONFLICT, ReasonCode.EVIDENCE_CONFLICT
        if primary_has_unknown or EvidenceStatus.UNKNOWN in supporting_statuses:
            return EvidenceStatus.UNKNOWN, ConflictLevel.NONE, ReasonCode.INSUFFICIENT_EVIDENCE
        return EvidenceStatus.NOT_MATCHED, ConflictLevel.NONE, _decision_negative_code(primary)

    if EvidenceStatus.MATCHED in supporting_statuses and EvidenceStatus.NOT_MATCHED in supporting_statuses:
        return EvidenceStatus.UNKNOWN, ConflictLevel.CONCLUSIVE_CONFLICT, ReasonCode.EVIDENCE_CONFLICT
    if EvidenceStatus.MATCHED in supporting_statuses:
        return EvidenceStatus.MATCHED, ConflictLevel.NONE, ReasonCode.MATCH_CONFIRMED
    if EvidenceStatus.NOT_MATCHED in supporting_statuses:
        if EvidenceStatus.UNKNOWN in supporting_statuses:
            return EvidenceStatus.UNKNOWN, ConflictLevel.NONE, ReasonCode.INSUFFICIENT_EVIDENCE
        return EvidenceStatus.NOT_MATCHED, ConflictLevel.NONE, _decision_negative_code(supporting)
    all_statuses = primary_statuses | supporting_statuses
    if EvidenceStatus.UNKNOWN not in all_statuses and EvidenceStatus.NOT_MENTIONED in all_statuses:
        return EvidenceStatus.NOT_MENTIONED, ConflictLevel.NONE, ReasonCode.NO_MATCHING_RECORD
    unknown_decisions = [
        item for item in primary + supporting
        if item.get('status') == EvidenceStatus.UNKNOWN.value
    ]
    complete_not_mentioned = any(
        item.get('status') == EvidenceStatus.NOT_MENTIONED.value
        and item.get('data_quality') == DataQuality.COMPLETE.value
        and item.get('selection_complete') is not False
        for item in primary + supporting
    )
    complete_primary_not_mentioned = any(
        item.get('status') == EvidenceStatus.NOT_MENTIONED.value
        and item.get('data_quality') == DataQuality.COMPLETE.value
        and item.get('selection_complete') is not False
        and item.get('source_role') == EvidenceRole.PRIMARY.value
        for item in primary + supporting
    )

    def _unknown_source_non_decisive(item: dict[str, Any]) -> bool:
        if (
            item.get('reason_code') == ReasonCode.SOURCE_UNAVAILABLE.value
            or item.get('data_quality') == DataQuality.SOURCE_ERROR.value
            or item.get('uncertainty_kind') == EvidenceUncertaintyKind.REJECTED_CANDIDATE.value
        ):
            return True
        return (
            complete_primary_not_mentioned
            and item.get('uncertainty_kind') in {
                EvidenceUncertaintyKind.INCOMPLETE_SEARCH.value,
                EvidenceUncertaintyKind.UNRESOLVED_CANDIDATE.value,
            }
            and item.get('source_role') != EvidenceRole.PRIMARY.value
        )

    unknown_sources_non_decisive = bool(unknown_decisions) and all(
        _unknown_source_non_decisive(item)
        for item in unknown_decisions
    )
    if complete_not_mentioned and unknown_sources_non_decisive:
        return EvidenceStatus.NOT_MENTIONED, ConflictLevel.NONE, ReasonCode.NO_MATCHING_RECORD
    unavailable = any(
        item.get('reason_code') == ReasonCode.SOURCE_UNAVAILABLE.value
        for item in primary + supporting
    )
    if unavailable:
        reason_code = ReasonCode.SOURCE_UNAVAILABLE
    else:
        unknown_codes = {
            item.get('reason_code')
            for item in primary + supporting
            if item.get('status') == EvidenceStatus.UNKNOWN.value
        }
        reason_code = next(
            (
                code for code in (
                    ReasonCode.MISSING_REQUIRED_CAPABILITY,
                    ReasonCode.SOURCE_ROLE_NOT_DECISIVE,
                    ReasonCode.INCOMPLETE_CANDIDATE_SET,
                )
                if code.value in unknown_codes
            ),
            ReasonCode.INSUFFICIENT_EVIDENCE,
        )
    return EvidenceStatus.UNKNOWN, ConflictLevel.NONE, reason_code


def _resolve_decision_statuses(
    decisions: list[dict[str, Any]],
) -> tuple[EvidenceStatus, ConflictLevel, ReasonCode]:
    policy = adjudicate_source_decisions(decisions)
    return (
        EvidenceStatus(policy.status),
        ConflictLevel(policy.conflict_level),
        ReasonCode(policy.reason_code),
    )


def _decision_negative_code(decisions: list[dict[str, Any]]) -> ReasonCode:
    codes = list(dict.fromkeys(item.get('reason_code') for item in decisions if item.get('reason_code')))
    if len(codes) == 1 and codes[0] in ReasonCode._value2member_map_:
        return ReasonCode(codes[0])
    return ReasonCode.NO_MATCHING_RECORD


def _resolved_reason(
    original_reason: str,
    original_status: EvidenceStatus,
    status: EvidenceStatus,
    conflict_level: ConflictLevel,
    decisions: list[dict[str, Any]],
) -> str:
    if conflict_level == ConflictLevel.CONCLUSIVE_CONFLICT:
        conflict_sources = [
            f"{item.get('source_name') or item.get('source_id')}={item.get('status')}"
            for item in decisions
            if item.get('source_role') not in {
                EvidenceRole.CONTEXT.value,
                EvidenceRole.TIME_ANCHOR.value,
            }
            and item.get('status') != EvidenceStatus.UNKNOWN.value
        ]
        detail = "、".join(conflict_sources[:4])
        return f"不同证据来源的确定性结论相互冲突，当前无法判断：{detail}"
    if conflict_level == ConflictLevel.SUPPORTING_DISAGREEMENT:
        primary_reasons = [
            item.get('reason', '')
            for item in decisions
            if item.get('source_role') == EvidenceRole.PRIMARY.value
            and item.get('status') == status.value
            and item.get('reason')
        ]
        base_reason = original_reason or "；".join(dict.fromkeys(primary_reasons))
        if base_reason:
            return f"{base_reason}；辅助证据存在分歧，未覆盖主证据结论"[:500]
        return "辅助证据存在分歧，主证据结论保持有效"
    if original_reason and original_status == status:
        return original_reason
    selected = [
        item.get('reason', '')
        for item in decisions
        if item.get('status') == status.value
        and item.get('source_role') not in {
            EvidenceRole.CONTEXT.value,
            EvidenceRole.TIME_ANCHOR.value,
        }
        and item.get('reason')
    ]
    if selected:
        return "；".join(dict.fromkeys(selected))[:500]
    if status == EvidenceStatus.NOT_MENTIONED:
        return "相关数据源查询成功，但未检索到目标实体或候选记录"
    if status == EvidenceStatus.MATCHED:
        return "存在满足条件的有效证据"
    if status == EvidenceStatus.NOT_MATCHED:
        return "有效证据不满足条件"
    return original_reason or "关键证据不足，当前无法判断"


def _condition_data_quality(
    status: EvidenceStatus,
    evidence: list[EvidenceItem],
    conflict_level: ConflictLevel,
) -> DataQuality:
    if not evidence:
        return DataQuality.MISSING
    if conflict_level == ConflictLevel.CONCLUSIVE_CONFLICT:
        return DataQuality.PARTIAL
    if status == EvidenceStatus.UNKNOWN and any(
        item.data_quality == DataQuality.SOURCE_ERROR for item in evidence
    ):
        return DataQuality.SOURCE_ERROR
    if all(item.data_quality == DataQuality.COMPLETE for item in evidence):
        return DataQuality.COMPLETE
    return DataQuality.PARTIAL


def adjudicate_condition(request: ConditionAdjudicationRequest) -> ConditionResult:
    """Resolve one atomic condition from canonical evidence only."""
    evidence = list(request.evidence)
    reason = sanitize_user_text(request.original_reason)
    original_status = request.original_status
    decisions = _source_decisions(evidence)
    if decisions:
        status, conflict_level, reason_code = _resolve_decision_statuses(decisions)
    else:
        status = original_status
        conflict_level = ConflictLevel.NONE
        reason_code = _reason_code(status, reason, request.original_reason_code)
    reason = _resolved_reason(
        reason,
        original_status,
        status,
        conflict_level,
        decisions,
    )
    data_quality = _condition_data_quality(status, evidence, conflict_level)
    return ConditionResult(
        condition_id=request.condition_id,
        condition=request.condition,
        status=status,
        reason_code=reason_code,
        reason=reason,
        data_quality=data_quality,
        evidence=evidence,
        conflict_level=conflict_level,
        source_decisions=decisions,
    )


def adjudicate_condition_result(
    condition_result: dict[str, Any],
    condition_id: str,
) -> ConditionResult:
    """Compatibility adapter from current Web/file dictionaries to canonical adjudication."""
    result = adjudicate_condition(ConditionAdjudicationRequest(
        condition_id=condition_id,
        condition=str(condition_result.get("condition", "") or ""),
        evidence=tuple(_collect_condition_evidence(condition_result, condition_id)),
        original_status=_evidence_status(condition_result),
        original_reason=sanitize_user_text(str(condition_result.get("reason", "") or "")),
        original_reason_code=str(condition_result.get("reason_code") or ""),
    ))
    consistency = condition_result.get("encounter_consistency")
    if isinstance(consistency, dict) and consistency.get("blocks_adjudication"):
        return ConditionResult(
            condition_id=result.condition_id,
            condition=result.condition,
            status=EvidenceStatus.UNKNOWN,
            reason_code=ReasonCode.ENCOUNTER_CONTEXT_CONFLICT,
            reason=sanitize_user_text(str(
                consistency.get("reason")
                or "???????????????????????"
            )),
            data_quality=DataQuality.PARTIAL,
            evidence=result.evidence,
            conflict_level=ConflictLevel.CONCLUSIVE_CONFLICT,
            source_decisions=result.source_decisions,
        )
    return result


def build_condition_result(condition_result: dict[str, Any], condition_id: str) -> ConditionResult:
    """Backward-compatible alias for callers that still provide legacy dictionaries."""
    return adjudicate_condition_result(condition_result, condition_id)


def sync_condition_result(
    condition_result: dict[str, Any],
    unified: ConditionResult | dict[str, Any],
) -> dict[str, Any]:
    data = unified.to_dict() if isinstance(unified, ConditionResult) else dict(unified)
    status = str(data.get('status') or EvidenceStatus.UNKNOWN.value)
    status_labels = {
        EvidenceStatus.NOT_MENTIONED.value: '\u672a\u63d0\u53ca',
        EvidenceStatus.MATCHED.value: '符合',
        EvidenceStatus.NOT_MATCHED.value: '不符合',
        EvidenceStatus.UNKNOWN.value: '无法判断',
    }
    condition_result['matched'] = status == EvidenceStatus.MATCHED.value
    condition_result['status'] = status
    condition_result['判断状态'] = status_labels.get(status, '无法判断')
    condition_result['可判定'] = status != EvidenceStatus.UNKNOWN.value
    condition_result['reason_code'] = str(data.get('reason_code') or '')
    condition_result['reason'] = str(data.get('reason') or condition_result.get('reason') or '')
    condition_result['conflict_level'] = str(data.get('conflict_level') or ConflictLevel.NONE.value)
    condition_result['source_decisions'] = list(data.get('source_decisions') or [])
    return condition_result


def enrich_response_with_evidence_model(response: dict[str, Any], query_ir: Any = None) -> dict[str, Any]:
    """Add the v1 evidence contract while retaining every legacy response field."""
    if not isinstance(response, dict):
        return response
    ir_data = query_ir.to_dict() if hasattr(query_ir, "to_dict") else (query_ir or {})
    ir_conditions = ir_data.get("子条件", []) if isinstance(ir_data, dict) else []
    connector = ir_data.get("连接关系") if isinstance(ir_data, dict) else None
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
            sync_condition_result(item, unified_dict)
            decisions_by_source = {
                str(decision.get("source_name") or ""): decision
                for decision in unified_dict.get("source_decisions", [])
                if isinstance(decision, dict) and decision.get("source_name")
            }
            for source_result in item.get("files", []) or []:
                if isinstance(source_result, dict):
                    source_result.pop("_structured_evidence_records", None)
                    source_decision = decisions_by_source.get(
                        str(source_result.get("file") or "")
                    )
                    if source_decision:
                        source_result["status"] = source_decision["status"]
                        source_result["reason_code"] = source_decision["reason_code"]
                        source_result["data_quality"] = source_decision["data_quality"]
            item["evidence_items"] = unified_dict["evidence"]
            item["condition_result"] = unified_dict
            normalized_conditions.append(unified_dict)
        patient_result["condition_results"] = normalized_conditions
        if normalized_conditions:
            overall_result = build_overall_result(normalized_conditions, connector=connector)
            patient_result["overall_result"] = overall_result
            patient_result["matched"] = overall_result["matched"]
            patient_result["判断状态"] = overall_result["判断状态"]
            patient_result["可判定"] = overall_result["conclusive"]
            patient_result["reason"] = overall_result["reason"]
        patient_result["evidence_model_version"] = "1.0"
    patient_results = [item for item in response.get("results", []) or [] if isinstance(item, dict)]
    if len(patient_results) == 1 and isinstance(patient_results[0].get("overall_result"), dict):
        overall_result = patient_results[0]["overall_result"]
        response["matched_count"] = 1 if overall_result["matched"] else 0
        response["判断状态"] = overall_result["判断状态"]
        response["可判定"] = overall_result["conclusive"]
        response["overall_result"] = overall_result
    response["evidence_model_version"] = "1.0"
    return response


def judgment_status(
    matched: bool,
    reason: str,
    per_condition: dict = None,
    *,
    use_and: bool = True,
) -> tuple[str, bool]:
    explicit_statuses = []
    status_aliases = {
        "NOT_MENTIONED": EvidenceStatus.NOT_MENTIONED,
        "未提及": EvidenceStatus.NOT_MENTIONED,
        "MATCHED": EvidenceStatus.MATCHED,
        "符合": EvidenceStatus.MATCHED,
        "NOT_MATCHED": EvidenceStatus.NOT_MATCHED,
        "不符合": EvidenceStatus.NOT_MATCHED,
        "UNKNOWN": EvidenceStatus.UNKNOWN,
        "无法判断": EvidenceStatus.UNKNOWN,
    }
    for item in (per_condition or {}).values():
        if not isinstance(item, dict):
            continue
        explicit = str(item.get("status") or item.get("判断状态") or "").upper()
        if explicit in status_aliases:
            explicit_statuses.append(status_aliases[explicit])
    if explicit_statuses:
        combined = combine_condition_statuses(explicit_statuses, use_and=use_and)
        return status_label(combined), combined != EvidenceStatus.UNKNOWN

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
    source_kind = str(file_result.get("source_kind") or "").strip().lower()
    source_type = str(file_result.get("source_type") or "").strip().lower()
    service_id = str(file_result.get("service_id") or "").strip()
    is_structured_service = bool(service_id) or source_kind == "service" or source_type == "service"

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
    elif is_structured_service:
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


def assess_patient_confidence(
    matched: bool,
    reason: str,
    per_condition: dict = None,
    *,
    use_and: bool = True,
) -> dict[str, Any]:
    reason = sanitize_user_text(str(reason or ""))
    status, conclusive = judgment_status(matched, reason, per_condition, use_and=use_and)
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
