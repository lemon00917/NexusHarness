"""Unified contracts and compatibility adapters for deterministic domains.

Domain rules still own their medical semantics. This module gives the
execution pipeline one immutable request shape, one four-state result shape,
and explicit evidence-capability diagnostics while legacy dictionary callers
continue to work.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
import re
from typing import Any, Callable, Mapping

from microharness.medical.evidence import (
    DataQuality,
    EvidenceRole,
    EvidenceStatus,
    infer_evidence_uncertainty_kind,
)
from microharness.medical.evidence_policy import resolve_source_role_policy
from microharness.medical.source_capability import (
    build_source_capability_profile,
)


class EvidenceCapability(str, Enum):
    """Generic facts an evidence source may prove deterministically."""

    ENTITY_PRESENCE = "ENTITY_PRESENCE"
    TEMPORAL_OCCURRENCE = "TEMPORAL_OCCURRENCE"
    NUMERIC_VALUE = "NUMERIC_VALUE"
    ABNORMALITY = "ABNORMALITY"
    REFERENCE_RANGE = "REFERENCE_RANGE"
    ORDER_EVENT = "ORDER_EVENT"
    ADMINISTRATION_EVENT = "ADMINISTRATION_EVENT"
    STATUS_VALIDITY = "STATUS_VALIDITY"
    DIAGNOSIS_ASSERTION = "DIAGNOSIS_ASSERTION"
    OUTCOME_STATE = "OUTCOME_STATE"
    HISTORY_DURATION = "HISTORY_DURATION"
    SUBJECT_ATTRIBUTION = "SUBJECT_ATTRIBUTION"
    ENCOUNTER_PERIOD = "ENCOUNTER_PERIOD"
    DOCUMENT_CONTEXT = "DOCUMENT_CONTEXT"


class ConditionSemanticType(str, Enum):
    """Stable condition classes used to select evidence requirements."""

    ENTITY_PRESENCE = "ENTITY_PRESENCE"
    LAB_NUMERIC = "LAB_NUMERIC"
    LAB_ABNORMALITY = "LAB_ABNORMALITY"
    MEDICATION_ORDER = "MEDICATION_ORDER"
    MEDICATION_ADMINISTRATION = "MEDICATION_ADMINISTRATION"
    DIAGNOSIS_ASSERTION = "DIAGNOSIS_ASSERTION"
    HISTORY_PRESENCE = "HISTORY_PRESENCE"
    HISTORY_DURATION = "HISTORY_DURATION"
    OUTCOME_STATE = "OUTCOME_STATE"
    NUMERIC_COMPARISON = "NUMERIC_COMPARISON"
    ENCOUNTER_DURATION = "ENCOUNTER_DURATION"


_DECISION_SOURCE_ROLES = (
    EvidenceRole.PRIMARY,
    EvidenceRole.SUPPORTING,
    EvidenceRole.CANDIDATE,
)


@dataclass(frozen=True)
class EvidenceRequirement:
    """Evidence contract for one semantic condition and source domain."""

    semantic_type: ConditionSemanticType
    evidence_domain: str
    required_capabilities: tuple[EvidenceCapability, ...]
    acceptable_source_roles: tuple[EvidenceRole, ...] = _DECISION_SOURCE_ROLES


_DOCUMENT_ASSERTION_CAPABILITIES = (
    EvidenceCapability.ENTITY_PRESENCE,
    EvidenceCapability.DOCUMENT_CONTEXT,
    EvidenceCapability.SUBJECT_ATTRIBUTION,
)


def _requirement(
    semantic_type: ConditionSemanticType,
    evidence_domain: str,
    *capabilities: EvidenceCapability,
) -> EvidenceRequirement:
    return EvidenceRequirement(
        semantic_type=semantic_type,
        evidence_domain=evidence_domain,
        required_capabilities=tuple(capabilities),
    )


_EVIDENCE_REQUIREMENT_REGISTRY: dict[
    tuple[ConditionSemanticType, str], EvidenceRequirement
] = {
    (ConditionSemanticType.ENTITY_PRESENCE, "document"): _requirement(
        ConditionSemanticType.ENTITY_PRESENCE,
        "document",
        *_DOCUMENT_ASSERTION_CAPABILITIES,
    ),
    (ConditionSemanticType.LAB_NUMERIC, "laboratory"): _requirement(
        ConditionSemanticType.LAB_NUMERIC,
        "laboratory",
        EvidenceCapability.ENTITY_PRESENCE,
        EvidenceCapability.NUMERIC_VALUE,
    ),
    (ConditionSemanticType.LAB_ABNORMALITY, "laboratory"): _requirement(
        ConditionSemanticType.LAB_ABNORMALITY,
        "laboratory",
        EvidenceCapability.ENTITY_PRESENCE,
        EvidenceCapability.ABNORMALITY,
    ),
    (ConditionSemanticType.MEDICATION_ORDER, "medication"): _requirement(
        ConditionSemanticType.MEDICATION_ORDER,
        "medication",
        EvidenceCapability.ENTITY_PRESENCE,
        EvidenceCapability.ORDER_EVENT,
    ),
    (ConditionSemanticType.MEDICATION_ADMINISTRATION, "medication"): _requirement(
        ConditionSemanticType.MEDICATION_ADMINISTRATION,
        "medication",
        EvidenceCapability.ENTITY_PRESENCE,
        EvidenceCapability.ADMINISTRATION_EVENT,
    ),
    (ConditionSemanticType.DIAGNOSIS_ASSERTION, "diagnosis"): _requirement(
        ConditionSemanticType.DIAGNOSIS_ASSERTION,
        "diagnosis",
        EvidenceCapability.ENTITY_PRESENCE,
        EvidenceCapability.DIAGNOSIS_ASSERTION,
    ),
    (ConditionSemanticType.DIAGNOSIS_ASSERTION, "document"): _requirement(
        ConditionSemanticType.DIAGNOSIS_ASSERTION,
        "document",
        *_DOCUMENT_ASSERTION_CAPABILITIES,
    ),
    (ConditionSemanticType.HISTORY_PRESENCE, "diagnosis"): _requirement(
        ConditionSemanticType.HISTORY_PRESENCE,
        "diagnosis",
        EvidenceCapability.ENTITY_PRESENCE,
        EvidenceCapability.DIAGNOSIS_ASSERTION,
    ),
    (ConditionSemanticType.HISTORY_PRESENCE, "document"): _requirement(
        ConditionSemanticType.HISTORY_PRESENCE,
        "document",
        *_DOCUMENT_ASSERTION_CAPABILITIES,
    ),
    (ConditionSemanticType.HISTORY_DURATION, "diagnosis"): _requirement(
        ConditionSemanticType.HISTORY_DURATION,
        "diagnosis",
        EvidenceCapability.ENTITY_PRESENCE,
        EvidenceCapability.DIAGNOSIS_ASSERTION,
        EvidenceCapability.HISTORY_DURATION,
    ),
    (ConditionSemanticType.HISTORY_DURATION, "document"): _requirement(
        ConditionSemanticType.HISTORY_DURATION,
        "document",
        *_DOCUMENT_ASSERTION_CAPABILITIES,
        EvidenceCapability.HISTORY_DURATION,
    ),
    (ConditionSemanticType.OUTCOME_STATE, "diagnosis"): _requirement(
        ConditionSemanticType.OUTCOME_STATE,
        "diagnosis",
        EvidenceCapability.ENTITY_PRESENCE,
        EvidenceCapability.DIAGNOSIS_ASSERTION,
        EvidenceCapability.OUTCOME_STATE,
    ),
    (ConditionSemanticType.OUTCOME_STATE, "document"): _requirement(
        ConditionSemanticType.OUTCOME_STATE,
        "document",
        *_DOCUMENT_ASSERTION_CAPABILITIES,
        EvidenceCapability.OUTCOME_STATE,
    ),
    (ConditionSemanticType.NUMERIC_COMPARISON, "numeric"): _requirement(
        ConditionSemanticType.NUMERIC_COMPARISON,
        "numeric",
        EvidenceCapability.NUMERIC_VALUE,
    ),
    (ConditionSemanticType.NUMERIC_COMPARISON, "demographic"): _requirement(
        ConditionSemanticType.NUMERIC_COMPARISON,
        "demographic",
        EvidenceCapability.NUMERIC_VALUE,
    ),
    (ConditionSemanticType.ENCOUNTER_DURATION, "encounter"): _requirement(
        ConditionSemanticType.ENCOUNTER_DURATION,
        "encounter",
        EvidenceCapability.ENCOUNTER_PERIOD,
        EvidenceCapability.NUMERIC_VALUE,
        EvidenceCapability.TEMPORAL_OCCURRENCE,
    ),
}


def _string_tuple(values: Any) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple, set)):
        return ()
    return tuple(dict.fromkeys(
        str(value).strip() for value in values if str(value).strip()
    ))


def _quantifier_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if value is None:
        return {}
    return {
        "mode": str(getattr(value, "mode", "") or ""),
        "count": getattr(value, "count", None),
        "unit": str(getattr(value, "unit", "") or ""),
    }


def _capability_tuple(values: Any) -> tuple[EvidenceCapability, ...]:
    if not isinstance(values, (list, tuple, set)):
        return ()
    normalized: list[EvidenceCapability] = []
    for value in values:
        if isinstance(value, EvidenceCapability):
            capability = value
        else:
            text = str(value or "").strip().upper()
            if text not in EvidenceCapability._value2member_map_:
                continue
            capability = EvidenceCapability(text)
        if capability not in normalized:
            normalized.append(capability)
    return tuple(normalized)


def _role_tuple(values: Any) -> tuple[EvidenceRole, ...]:
    if not isinstance(values, (list, tuple, set)):
        return ()
    normalized: list[EvidenceRole] = []
    for value in values:
        if isinstance(value, EvidenceRole):
            role = value
        else:
            text = str(value or "").strip().upper()
            if text not in EvidenceRole._value2member_map_:
                continue
            role = EvidenceRole(text)
        if role not in normalized:
            normalized.append(role)
    return tuple(normalized)


@dataclass(frozen=True)
class DomainExecutionRequest:
    """Stable input shared by structured deterministic domain executors."""

    condition_id: str
    condition: str
    bindings: tuple[dict[str, Any], ...]
    domain: str = ""
    entity_type: str = ""
    entity: str = ""
    entity_candidates: tuple[str, ...] = ()
    predicate: str = ""
    semantic_class: str = ""
    modifiers: tuple[str, ...] = ()
    numeric_comparison: Mapping[str, Any] | None = None
    is_numeric: bool = False
    time_window: Any = None
    semantic: Mapping[str, Any] = field(default_factory=dict)
    temporal_semantics: Mapping[str, Any] = field(default_factory=dict)
    attributes: Mapping[str, Any] = field(default_factory=dict)
    quantifier: Mapping[str, Any] = field(default_factory=dict)
    history_context: bool = False
    internal_negation: bool = False
    is_outcome_condition: bool = False
    outcome_state: str = ""
    outcome_phase: str = ""
    diagnosis_phase_evidence_allowed: bool = False

    @property
    def numeric_execution_required(self) -> bool:
        return bool(
            self.is_numeric
            or self.numeric_comparison
            or self.predicate.strip().lower() == "compare"
        )

    @property
    def is_age_condition(self) -> bool:
        from microharness.medical.temporal_parser import normalize_time_unit

        comparison = self.numeric_comparison or {}
        subject = str(comparison.get("subject") or "").strip()
        unit = normalize_time_unit(str(comparison.get("unit") or "").strip())
        return bool(
            self.numeric_execution_required
            and (
                self.domain.strip().lower() == "demographic"
                or self.entity_type.strip().lower() in {"age", "demographic"}
                or subject == "年龄"
                or unit == "岁"
            )
        )

    def numeric_comparison_issue(self) -> str:
        from microharness.medical.condition_execution import validate_numeric_comparison

        return validate_numeric_comparison(
            self.numeric_comparison,
            required=self.numeric_execution_required,
            is_age_condition=self.is_age_condition,
        )

    @classmethod
    def from_execution_spec(
        cls,
        execution_spec: Any,
        bindings: list[dict[str, Any]] | tuple[dict[str, Any], ...],
        *,
        time_window: Any = None,
        semantic: Mapping[str, Any] | None = None,
        temporal_semantics: Mapping[str, Any] | None = None,
    ) -> "DomainExecutionRequest":
        return cls(
            condition_id=str(getattr(execution_spec, "condition_id", "") or ""),
            condition=str(getattr(execution_spec, "text", "") or ""),
            bindings=tuple(item for item in (bindings or ()) if isinstance(item, dict)),
            domain=str(getattr(execution_spec, "domain", "") or ""),
            entity_type=str(getattr(execution_spec, "entity_type", "") or ""),
            entity=str(
                getattr(execution_spec, "canonical_entity", "")
                or getattr(execution_spec, "entity", "")
                or getattr(execution_spec, "keyword", "")
                or ""
            ),
            entity_candidates=_string_tuple(
                getattr(execution_spec, "entity_candidates", ())
            ),
            predicate=str(getattr(execution_spec, "predicate", "") or ""),
            semantic_class=str(
                getattr(execution_spec, "semantic_class", "") or ""
            ),
            modifiers=_string_tuple(getattr(execution_spec, "modifiers", ())),
            numeric_comparison=getattr(execution_spec, "numeric_comparison", None),
            is_numeric=bool(
                getattr(execution_spec, "numeric_execution_required", False)
            ),
            time_window=time_window,
            semantic=dict(semantic or {}),
            temporal_semantics=dict(temporal_semantics or {}),
            attributes=dict(getattr(execution_spec, "attributes", {}) or {}),
            quantifier=_quantifier_mapping(
                getattr(execution_spec, "quantifier", None)
            ),
            history_context=bool(
                getattr(execution_spec, "history_context", False)
            ),
            internal_negation=bool(
                getattr(execution_spec, "internal_negation", False)
            ),
            is_outcome_condition=bool(
                getattr(execution_spec, "is_outcome_condition", False)
            ),
            outcome_state=str(
                getattr(execution_spec, "outcome_state", "") or ""
            ),
            outcome_phase=str(
                getattr(execution_spec, "outcome_phase", "") or ""
            ),
            diagnosis_phase_evidence_allowed=bool(
                getattr(execution_spec, "diagnosis_phase_evidence_allowed", False)
            ),
        )


def _has_history_duration_requirement(request: DomainExecutionRequest) -> bool:
    attributes = request.attributes
    if any(
        attributes.get(key) not in (None, "", False)
        for key in ("history_duration", "history_years", "duration_years")
    ):
        return True
    return any(
        "病史" in modifier and "年" in modifier
        for modifier in request.modifiers
    )


def resolve_condition_semantic_type(
    request: DomainExecutionRequest,
) -> ConditionSemanticType:
    """Resolve the execution class from IR fields without reparsing the query."""
    domain = request.domain.strip().lower()
    entity_type = request.entity_type.strip().lower()
    predicate = request.predicate.strip().lower()

    if request.is_outcome_condition or request.outcome_state:
        return ConditionSemanticType.OUTCOME_STATE
    if request.history_context:
        if _has_history_duration_requirement(request):
            return ConditionSemanticType.HISTORY_DURATION
        return ConditionSemanticType.HISTORY_PRESENCE
    if domain == "encounter" or entity_type in {"duration", "encounter"}:
        if request.numeric_execution_required:
            return ConditionSemanticType.ENCOUNTER_DURATION
    if domain == "laboratory" or entity_type in {"lab", "laboratory"}:
        if request.numeric_execution_required:
            return ConditionSemanticType.LAB_NUMERIC
        return ConditionSemanticType.LAB_ABNORMALITY
    if domain == "medication" or entity_type in {"drug", "medication"}:
        return (
            ConditionSemanticType.MEDICATION_ADMINISTRATION
            if _medication_predicate(request) == "administered"
            else ConditionSemanticType.MEDICATION_ORDER
        )
    if domain == "diagnosis" or entity_type == "diagnosis":
        return ConditionSemanticType.DIAGNOSIS_ASSERTION
    if request.numeric_execution_required or predicate == "compare":
        return ConditionSemanticType.NUMERIC_COMPARISON
    return ConditionSemanticType.ENTITY_PRESENCE


def resolve_evidence_requirement(
    request: DomainExecutionRequest,
    evidence_domain: str,
) -> EvidenceRequirement:
    """Return the generic proof contract for this condition/source pair."""
    semantic_type = resolve_condition_semantic_type(request)
    domain = str(evidence_domain or "").strip().lower() or "document"
    requirement = _EVIDENCE_REQUIREMENT_REGISTRY.get((semantic_type, domain))
    if requirement is not None:
        return requirement
    if domain == "document":
        return _requirement(
            semantic_type,
            domain,
            *_DOCUMENT_ASSERTION_CAPABILITIES,
        )
    return _requirement(
        semantic_type,
        domain,
        EvidenceCapability.ENTITY_PRESENCE,
    )


@dataclass(frozen=True)
class DomainExecutionResult:
    """Canonical result returned by every deterministic domain adapter."""

    applicable: bool
    domain: str = ""
    executor: str = ""
    status: EvidenceStatus = EvidenceStatus.UNKNOWN
    reason_code: str = "INSUFFICIENT_EVIDENCE"
    reason: str = ""
    data_quality: DataQuality = DataQuality.MISSING
    fields: str = ""
    candidate_count: int = 0
    candidate_records: tuple[dict[str, Any], ...] = ()
    quantifier_mode: str = ""
    quantifier_count: float | None = None
    quantifier_unit: str = ""
    selected_candidate_records: tuple[dict[str, Any], ...] = ()
    record_status_counts: Mapping[str, int] = field(default_factory=dict)
    selection_complete: bool = True
    semantic_type: ConditionSemanticType = ConditionSemanticType.ENTITY_PRESENCE
    supported_capabilities: tuple[EvidenceCapability, ...] = ()
    required_capabilities: tuple[EvidenceCapability, ...] = ()
    missing_capabilities: tuple[EvidenceCapability, ...] = ()
    acceptable_source_roles: tuple[EvidenceRole, ...] = _DECISION_SOURCE_ROLES
    source_role_acceptable: bool | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)

    @property
    def matched(self) -> bool:
        return self.applicable and self.status == EvidenceStatus.MATCHED

    @property
    def conclusive(self) -> bool:
        return self.applicable and self.status != EvidenceStatus.UNKNOWN

    def to_legacy_dict(self) -> dict[str, Any]:
        if not self.applicable:
            return {"applicable": False, **dict(self.extra)}
        data = dict(self.extra)
        data.update({
            "applicable": True,
            "matched": self.matched,
            "conclusive": self.conclusive,
            "status": self.status.value,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "data_quality": self.data_quality.value,
            "fields": self.fields,
            "candidate_count": self.candidate_count,
            "candidate_records": [dict(item) for item in self.candidate_records],
            "quantifier_mode": self.quantifier_mode,
            "quantifier_count": self.quantifier_count,
            "quantifier_unit": self.quantifier_unit,
            "selected_candidate_records": [
                dict(item) for item in self.selected_candidate_records
            ],
            "record_status_counts": dict(self.record_status_counts),
            "selection_complete": self.selection_complete,
            "domain": self.domain,
            "executor": self.executor,
            "semantic_type": self.semantic_type.value,
            "supported_capabilities": [item.value for item in self.supported_capabilities],
            "required_capabilities": [item.value for item in self.required_capabilities],
            "missing_capabilities": [item.value for item in self.missing_capabilities],
            "acceptable_source_roles": [item.value for item in self.acceptable_source_roles],
            "source_role_acceptable": self.source_role_acceptable,
        })
        return data

    def to_file_result(
        self,
        file_name: str,
        *,
        fallback_fields: str = "",
        default_reason: str = "结构化判断完成",
    ) -> dict[str, Any]:
        data = self.to_legacy_dict()
        result = {
            "file": file_name,
            "matched": bool(data.get("matched")),
            "status": data.get("status"),
            "reason_code": data.get("reason_code"),
            "reason": data.get("reason") or default_reason,
            "data_quality": data.get("data_quality"),
            "fields": str(data.get("fields") or fallback_fields)[:4000],
            "cot_response": "",
            "domain": data.get("domain", ""),
            "executor": data.get("executor", ""),
            "semantic_type": data.get("semantic_type", ""),
            "supported_capabilities": data.get("supported_capabilities", []),
            "required_capabilities": data.get("required_capabilities", []),
            "missing_capabilities": data.get("missing_capabilities", []),
            "acceptable_source_roles": data.get("acceptable_source_roles", []),
            "source_role_acceptable": data.get("source_role_acceptable"),
            "quantifier_mode": data.get("quantifier_mode", ""),
            "quantifier_count": data.get("quantifier_count"),
            "quantifier_unit": data.get("quantifier_unit", ""),
            "record_status_counts": data.get("record_status_counts", {}),
            "selection_complete": data.get("selection_complete", True),
            "uncertainty_kind": data.get("uncertainty_kind", ""),
        }
        candidates = data.get("candidate_records") or []
        if candidates:
            result["candidate_records"] = candidates
            result["candidate_count"] = data.get("candidate_count", len(candidates))
            result["候选记录"] = candidates
            result["候选记录数"] = data.get("candidate_count", len(candidates))
        selected = data.get("selected_candidate_records") or []
        if selected:
            result["selected_candidate_records"] = selected
            result["量词选中记录"] = selected
        if data.get("semantic_trace") is not None:
            result["semantic_trace"] = data.get("semantic_trace")
        return result


def _status_from_raw(raw: Mapping[str, Any]) -> EvidenceStatus:
    explicit = str(raw.get("status") or "").strip().upper()
    aliases = {
        "符合": EvidenceStatus.MATCHED,
        "不符合": EvidenceStatus.NOT_MATCHED,
        "未提及": EvidenceStatus.NOT_MENTIONED,
        "无法判断": EvidenceStatus.UNKNOWN,
    }
    if explicit in EvidenceStatus._value2member_map_:
        return EvidenceStatus(explicit)
    if explicit in aliases:
        return aliases[explicit]
    return EvidenceStatus.MATCHED if bool(raw.get("matched")) else EvidenceStatus.NOT_MATCHED


def _data_quality_from_raw(
    raw: Mapping[str, Any],
    status: EvidenceStatus,
    reason_code: str,
) -> DataQuality:
    explicit = str(raw.get("data_quality") or "").strip().upper()
    if explicit in DataQuality._value2member_map_:
        return DataQuality(explicit)
    if reason_code == "SOURCE_UNAVAILABLE":
        return DataQuality.SOURCE_ERROR
    if status == EvidenceStatus.UNKNOWN:
        return DataQuality.PARTIAL if raw.get("candidate_records") else DataQuality.MISSING
    return DataQuality.COMPLETE


def _medication_predicate(request: DomainExecutionRequest) -> str:
    from microharness.medical.medication_rules import infer_medication_predicate

    configured = str(request.semantic.get("predicate") or request.predicate or "")
    return infer_medication_predicate(request.condition, configured)


def _medication_policy(request: DomainExecutionRequest) -> Mapping[str, Any]:
    policies = request.semantic.get("predicate_policies")
    if not isinstance(policies, Mapping):
        return {}
    policy = policies.get(_medication_predicate(request))
    return policy if isinstance(policy, Mapping) else {}


def _capability_contract(
    request: DomainExecutionRequest,
    domain: str,
    raw: Mapping[str, Any],
) -> tuple[
    tuple[EvidenceCapability, ...],
    tuple[EvidenceCapability, ...],
    tuple[EvidenceCapability, ...],
]:
    requirement = resolve_evidence_requirement(request, domain)
    window_required = bool(
        request.time_window is not None
        and getattr(request.time_window, "required", False)
    )
    reason_code = str(raw.get("reason_code") or "").strip().upper()
    predicate = _medication_predicate(request) if domain == "medication" else request.predicate
    policy = _medication_policy(request) if domain == "medication" else {}
    profile = build_source_capability_profile(
        domain=domain,
        semantic_type=requirement.semantic_type.value,
        required_capabilities=requirement.required_capabilities,
        semantic=request.semantic,
        raw=raw,
        predicate=predicate,
        policy=policy,
        numeric_required=bool(request.is_numeric),
        has_numeric_comparison=bool(request.numeric_comparison),
        has_modifiers=bool(request.modifiers),
        time_window_required=window_required,
        diagnosis_phase_evidence_allowed=request.diagnosis_phase_evidence_allowed,
        candidate_records=raw.get("candidate_records") or (),
        reason_code=reason_code,
    )
    return (
        _capability_tuple(profile.supported_capabilities),
        _capability_tuple(profile.required_capabilities),
        _capability_tuple(profile.missing_capabilities),
    )



def normalize_domain_result(
    raw: Mapping[str, Any],
    request: DomainExecutionRequest,
    *,
    domain: str,
    executor: str,
) -> DomainExecutionResult:
    """Normalize a legacy rule dictionary without changing its conclusion."""
    if not raw.get("applicable"):
        return DomainExecutionResult(
            applicable=False,
            domain=domain,
            executor=executor,
            extra={key: value for key, value in raw.items() if key != "applicable"},
        )

    status = _status_from_raw(raw)
    reason = str(raw.get("reason") or "")
    reason_code = str(raw.get("reason_code") or "").strip().upper()
    if not reason_code:
        reason_code = {
            EvidenceStatus.MATCHED: "MATCH_CONFIRMED",
            EvidenceStatus.NOT_MATCHED: "VALUE_CONDITION_NOT_MET",
            EvidenceStatus.NOT_MENTIONED: "NO_MATCHING_RECORD",
            EvidenceStatus.UNKNOWN: "INSUFFICIENT_EVIDENCE",
        }[status]
    candidate_records = tuple(
        dict(item) for item in (raw.get("candidate_records") or ())
        if isinstance(item, Mapping)
    )
    selected_candidate_records = tuple(
        dict(item) for item in (raw.get("selected_candidate_records") or ())
        if isinstance(item, Mapping)
    )
    raw_status_counts = raw.get("record_status_counts")
    record_status_counts = dict(raw_status_counts) if isinstance(raw_status_counts, Mapping) else {}
    explicit_count = raw.get("candidate_count")
    try:
        candidate_count = max(int(explicit_count), len(candidate_records))
    except (TypeError, ValueError):
        candidate_count = len(candidate_records)

    quantifier_decision = None
    document_quantifier_allowed = bool(
        domain == "document"
        and raw.get("allow_document_quantifier")
        and candidate_records
    )
    if (
        domain in {"laboratory", "medication", "diagnosis"}
        or document_quantifier_allowed
    ) and request.quantifier:
        from microharness.medical.record_selection import (
            adjudicate_record_quantifier,
        )

        complete_value = raw.get("candidate_records_complete", True)
        records_complete = not (
            complete_value is False
            or str(complete_value).strip().lower() in {"0", "false", "no"}
        )
        quantifier_decision = adjudicate_record_quantifier(
            candidate_records,
            request.quantifier,
            fallback_status=status,
            candidate_count=candidate_count,
            records_complete=records_complete,
        )
        if quantifier_decision.applicable:
            status = quantifier_decision.status
            reason_code = quantifier_decision.reason_code
            reason = quantifier_decision.reason

    requirement = resolve_evidence_requirement(request, domain)
    supported, required, missing = _capability_contract(request, domain, raw)
    if status == EvidenceStatus.MATCHED and missing:
        status = EvidenceStatus.UNKNOWN
        reason_code = "MISSING_REQUIRED_CAPABILITY"
        missing_text = "、".join(item.value for item in missing)
        reason = (
            f"{reason}；" if reason else ""
        ) + f"当前来源缺少完成该条件判断所需的证据能力：{missing_text}"

    role_value = str(
        raw.get("source_role")
        or raw.get("evidence_role")
        or request.semantic.get("source_role")
        or request.semantic.get("evidence_role")
        or ""
    ).strip().upper()
    source_role = (
        EvidenceRole(role_value)
        if role_value in EvidenceRole._value2member_map_
        else None
    )
    acceptable_roles = _role_tuple(
        raw.get("acceptable_source_roles")
    ) or requirement.acceptable_source_roles
    source_role_acceptable = (
        source_role in acceptable_roles if source_role is not None else None
    )
    if source_role is not None:
        role_policy = resolve_source_role_policy(
            raw=raw,
            semantic=request.semantic,
            semantic_type=requirement.semantic_type.value,
            acceptable_roles=acceptable_roles,
            missing_capabilities=missing,
        )
        source_role_acceptable = role_policy.acceptable
    if status == EvidenceStatus.MATCHED and source_role_acceptable is False:
        status = EvidenceStatus.UNKNOWN
        reason_code = "SOURCE_ROLE_NOT_DECISIVE"
        reason = (
            f"{reason}；" if reason else ""
        ) + "当前来源角色仅可提供上下文或时间锚点，不能独立证明该条件"
    canonical_keys = {
        "applicable", "matched", "conclusive", "status", "reason_code", "reason",
        "data_quality", "fields", "candidate_count", "candidate_records", "domain",
        "executor", "supported_capabilities", "required_capabilities", "missing_capabilities",
        "semantic_type", "acceptable_source_roles", "source_role_acceptable",
        "candidate_records_complete", "allow_document_quantifier",
        "selected_candidate_records", "record_status_counts", "selection_complete",
    }
    return DomainExecutionResult(
        applicable=True,
        domain=domain,
        executor=executor,
        status=status,
        reason_code=reason_code,
        reason=reason,
        data_quality=_data_quality_from_raw(raw, status, reason_code),
        fields=str(raw.get("fields") or ""),
        candidate_count=candidate_count,
        candidate_records=candidate_records,
        quantifier_mode=(quantifier_decision.mode if quantifier_decision else ""),
        quantifier_count=(quantifier_decision.count if quantifier_decision else None),
        quantifier_unit=(quantifier_decision.unit if quantifier_decision else ""),
        selected_candidate_records=(
            quantifier_decision.selected_records if quantifier_decision else selected_candidate_records
        ),
        record_status_counts=(
            quantifier_decision.record_status_counts if quantifier_decision else record_status_counts
        ),
        selection_complete=(
            quantifier_decision.selection_complete
            if quantifier_decision
            else bool(raw.get("selection_complete", True))
        ),
        semantic_type=requirement.semantic_type,
        supported_capabilities=supported,
        required_capabilities=required,
        missing_capabilities=missing,
        acceptable_source_roles=acceptable_roles,
        source_role_acceptable=source_role_acceptable,
        extra={key: value for key, value in raw.items() if key not in canonical_keys},
    )


DomainAdapter = Callable[[DomainExecutionRequest], Mapping[str, Any]]


def _lab_adapter(request: DomainExecutionRequest) -> Mapping[str, Any]:
    from microharness.medical.lab_rules import judge_lab_condition

    return judge_lab_condition(
        request.condition,
        list(request.bindings),
        time_window=request.time_window,
        entity_candidates=list(request.entity_candidates),
    )


def _medication_adapter(request: DomainExecutionRequest) -> Mapping[str, Any]:
    from microharness.medical.medication_rules import judge_medication_condition

    semantic = dict(request.semantic)
    semantic.setdefault("domain", request.domain)
    semantic.setdefault("entity_type", request.entity_type)
    return judge_medication_condition(
        request.condition,
        list(request.bindings),
        entity=request.entity,
        entity_candidates=list(request.entity_candidates),
        time_window=request.time_window,
        semantic=semantic,
    )


def _diagnosis_adapter(request: DomainExecutionRequest) -> Mapping[str, Any]:
    from microharness.medical.diagnosis_rules import judge_diagnosis_condition

    semantic = dict(request.semantic)
    semantic.setdefault("domain", request.domain)
    semantic.setdefault("entity_type", request.entity_type)
    return judge_diagnosis_condition(
        request.condition,
        list(request.bindings),
        entity=request.entity,
        entity_candidates=list(request.entity_candidates),
        time_window=request.time_window,
        semantic=semantic,
        temporal_semantics=dict(request.temporal_semantics),
    )


_STRUCTURED_ADAPTERS: tuple[tuple[str, str, DomainAdapter], ...] = (
    ("laboratory", "lab_rules", _lab_adapter),
    ("medication", "medication_rules", _medication_adapter),
    ("diagnosis", "diagnosis_rules", _diagnosis_adapter),
)


def execute_structured_domain(
    request: DomainExecutionRequest,
    *,
    on_error: Callable[[str, Exception], None] | None = None,
) -> DomainExecutionResult | None:
    """Run the first applicable structured executor in compatibility order."""
    for domain, executor, adapter in _STRUCTURED_ADAPTERS:
        try:
            raw = adapter(request)
        except Exception as exc:
            if on_error is not None:
                on_error(executor, exc)
            continue
        if raw.get("applicable"):
            return normalize_domain_result(
                raw,
                request,
                domain=domain,
                executor=executor,
            )
    return None


def _numeric_domain(request: DomainExecutionRequest) -> str:
    domain = request.domain.strip().lower()
    entity_type = request.entity_type.strip().lower()
    if domain == "encounter" or entity_type in {"duration", "encounter"}:
        return "encounter"
    if domain == "demographic" or entity_type in {"age", "demographic"}:
        return "demographic"
    return domain or "numeric"


def _numeric_capabilities(
    domain: str,
) -> tuple[EvidenceCapability, ...]:
    if domain == "encounter":
        return (
            EvidenceCapability.ENCOUNTER_PERIOD,
            EvidenceCapability.NUMERIC_VALUE,
            EvidenceCapability.TEMPORAL_OCCURRENCE,
        )
    return (EvidenceCapability.NUMERIC_VALUE,)


def _strip_record_prefix(label: str) -> str:
    text = str(label or "").strip()
    match = re.match(r"^\[[^\]]+\]\s*(.+)$", text)
    return match.group(1).strip() if match else text


def _binding_record_key(binding: Mapping[str, Any], fallback: str) -> str:
    for key in ("record_id", "recordId", "id"):
        value = str(binding.get(key) or "").strip()
        if value:
            return value
    label = str(binding.get("html_field") or "").strip()
    match = re.match(r"^\[([^\]]+)\]", label)
    if match:
        return match.group(1).strip()
    return fallback


def _binding_datetime_role(binding: Mapping[str, Any]) -> str:
    tokens = " ".join(
        str(binding.get(key) or "")
        for key in ("html_field", "eng_field", "field_name", "fieldName", "xml_path")
    )
    normalized = tokens.casefold()
    start_tokens = (
        "encstart", "admission", "admit",
        "\u5165\u9662\u65e5\u671f", "\u5165\u9662\u65f6\u95f4", "\u5165\u9662\u65e5\u671f\u65f6\u95f4",
    )
    end_tokens = (
        "encend", "discharge", "discharged",
        "\u51fa\u9662\u65e5\u671f", "\u51fa\u9662\u65f6\u95f4", "\u51fa\u9662\u65e5\u671f\u65f6\u95f4",
    )
    if any(token.casefold() in normalized for token in start_tokens):
        return "start"
    if any(token.casefold() in normalized for token in end_tokens):
        return "end"
    return ""


_DATETIME_RE = re.compile(r"(\d{4}-\d{2}-\d{2})(?:[ T]+(\d{2}:\d{2}(?::\d{2})?))?")


def _parse_binding_datetime(value: Any) -> datetime | None:
    match = _DATETIME_RE.search(str(value or ""))
    if not match:
        return None
    time_part = match.group(2) or "00:00:00"
    if len(time_part) == 5:
        time_part += ":00"
    try:
        return datetime.strptime(f"{match.group(1)} {time_part}", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _encounter_duration_value(delta_seconds: float, unit: str) -> float:
    unit = unit or "\u5929"
    if unit == "\u5206\u949f":
        return round(delta_seconds / 60, 2)
    if unit == "\u5c0f\u65f6":
        return round(delta_seconds / 3600, 2)
    if unit == "\u5468":
        return round(delta_seconds / (7 * 86400), 2)
    if unit == "\u6708":
        return round(delta_seconds / (30 * 86400), 2)
    return float(int(delta_seconds // 86400))


def _format_duration_value(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.2f}".rstrip("0").rstrip(".")


def _record_reference(record: Mapping[str, Any], fallback: str) -> str:
    record_id = str(record.get("record_id") or "").strip()
    label = str(record.get("record_id_label") or "").strip()
    if record_id and label:
        return f"{label}={record_id}"
    if record_id:
        return record_id
    return fallback


def _encounter_duration_records(bindings: tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for index, binding in enumerate(bindings or (), start=1):
        if not isinstance(binding, Mapping):
            continue
        role = _binding_datetime_role(binding)
        if not role:
            continue
        value = binding.get("html_value") if binding.get("html_value") not in (None, "") else binding.get("value")
        parsed = _parse_binding_datetime(value)
        key = _binding_record_key(binding, f"record{index}")
        record = records.setdefault(key, {
            "record_key": key,
            "record_id": str(binding.get("record_id") or "").strip(),
            "record_id_label": str(binding.get("record_id_label") or "").strip(),
            "record_id_field": str(binding.get("record_id_field") or "").strip(),
            "fields": {},
            "start": None,
            "end": None,
        })
        label = _strip_record_prefix(str(binding.get("html_field") or binding.get("xml_path") or role))
        if value not in (None, ""):
            record["fields"][label] = str(value)
        if parsed is not None:
            record[role] = parsed
    return list(records.values())


def _execute_encounter_duration_records(
    request: DomainExecutionRequest,
    *,
    capabilities: tuple[EvidenceCapability, ...],
    fields: str,
) -> DomainExecutionResult | None:
    if _numeric_domain(request) != "encounter":
        return None
    records = _encounter_duration_records(request.bindings)
    if not records:
        return None

    from microharness.medical.temporal_parser import (
        compare_values,
        normalize_time_unit,
        operator_display,
    )

    comparison = request.numeric_comparison or {}
    subject = str(comparison.get("subject") or request.entity or request.condition or "").strip()
    operator = str(comparison.get("operator") or comparison.get("op") or "").strip()
    threshold = float(comparison["threshold"])
    unit = normalize_time_unit(str(comparison.get("unit") or "").strip()) or "\u5929"
    evaluated: list[dict[str, Any]] = []
    incomplete: list[dict[str, Any]] = []

    for index, record in enumerate(records, start=1):
        reference = _record_reference(record, f"record{index}")
        start = record.get("start")
        end = record.get("end")
        raw_text = "\n".join(
            f"  {name}: {value}" for name, value in record.get("fields", {}).items()
        )
        base = {
            "record_id": record.get("record_id") or record.get("record_key") or "",
            "record_id_label": record.get("record_id_label") or "",
            "record_id_field": record.get("record_id_field") or "",
            "document": "\u5c31\u8bca\u4fe1\u606f\u67e5\u8be2",
            "source_type": "encounter",
            "entity": subject,
            "raw_text": raw_text,
            "metadata": {
                "record_key": record.get("record_key") or "",
                "fields": dict(record.get("fields") or {}),
            },
        }
        if start is None or end is None:
            missing = []
            if start is None:
                missing.append("\u5165\u9662\u65e5\u671f\u65f6\u95f4")
            if end is None:
                missing.append("\u51fa\u9662\u65e5\u671f\u65f6\u95f4")
            incomplete.append(base | {
                "status": EvidenceStatus.UNKNOWN.value,
                "reason_code": "MISSING_NUMERIC_EVIDENCE",
                "data_quality": DataQuality.PARTIAL.value,
                "reason": f"{reference}\u7f3a\u5c11{', '.join(missing)}\uff0c\u65e0\u6cd5\u8ba1\u7b97\u4f4f\u9662\u5929\u6570",
            })
            continue
        delta_seconds = (end - start).total_seconds()
        if delta_seconds < 0:
            incomplete.append(base | {
                "status": EvidenceStatus.UNKNOWN.value,
                "reason_code": "MISSING_NUMERIC_EVIDENCE",
                "data_quality": DataQuality.PARTIAL.value,
                "reason": f"{reference}\u51fa\u9662\u65f6\u95f4\u65e9\u4e8e\u5165\u9662\u65f6\u95f4\uff0c\u65e0\u6cd5\u8ba1\u7b97\u4f4f\u9662\u5929\u6570",
            })
            continue
        value = _encounter_duration_value(delta_seconds, unit)
        matched = compare_values(value, operator, threshold)
        if matched is None:
            return None
        value_text = _format_duration_value(value)
        threshold_text = _format_duration_value(threshold)
        outcome_text = "\u7b26\u5408" if matched else "\u4e0d\u7b26\u5408"
        reason = (
            f"{reference}: {subject} = {value_text}{unit} "
            f"{operator_display(operator)} {threshold_text}{unit} -> "
            f"{outcome_text}"
        )
        evaluated.append(base | {
            "status": EvidenceStatus.MATCHED.value if matched else EvidenceStatus.NOT_MATCHED.value,
            "reason_code": "NUMERIC_CONDITION_MET" if matched else "NUMERIC_CONDITION_NOT_MET",
            "data_quality": DataQuality.COMPLETE.value,
            "reason": reason,
            "event_time": start.strftime("%Y-%m-%d %H:%M:%S"),
            "value": value,
            "unit": unit,
            "metadata": base["metadata"] | {
                "encounter_start": start.strftime("%Y-%m-%d %H:%M:%S"),
                "encounter_end": end.strftime("%Y-%m-%d %H:%M:%S"),
            },
        })

    matched_records = [item for item in evaluated if item["status"] == EvidenceStatus.MATCHED.value]
    if matched_records:
        decision_records = matched_records
        status = EvidenceStatus.MATCHED
        reason_code = "NUMERIC_CONDITION_MET"
        reason = "\uff1b".join(item["reason"] for item in matched_records[:3])
        missing_capabilities: tuple[EvidenceCapability, ...] = ()
        data_quality = DataQuality.COMPLETE
    elif evaluated:
        decision_records = evaluated
        status = EvidenceStatus.NOT_MATCHED
        reason_code = "NUMERIC_CONDITION_NOT_MET"
        reason = "\uff1b".join(item["reason"] for item in evaluated[:3])
        missing_capabilities = ()
        data_quality = DataQuality.COMPLETE
    else:
        decision_records = incomplete
        status = EvidenceStatus.UNKNOWN
        reason_code = "MISSING_NUMERIC_EVIDENCE"
        reason = "\uff1b".join(item["reason"] for item in incomplete[:3]) or (
            "\u672a\u53d6\u5f97\u5b8c\u6574\u5165\u9662/\u51fa\u9662\u65f6\u95f4\uff0c\u65e0\u6cd5\u5224\u65ad\u4f4f\u9662\u5929\u6570"
        )
        missing_capabilities = (EvidenceCapability.ENCOUNTER_PERIOD,)
        data_quality = DataQuality.PARTIAL

    counts = {
        EvidenceStatus.MATCHED.value: len(matched_records),
        EvidenceStatus.NOT_MATCHED.value: len([item for item in evaluated if item["status"] == EvidenceStatus.NOT_MATCHED.value]),
        EvidenceStatus.UNKNOWN.value: len(incomplete),
    }
    return normalize_domain_result(
        {
            "applicable": True,
            "matched": status == EvidenceStatus.MATCHED,
            "status": status.value,
            "reason_code": reason_code,
            "data_quality": data_quality.value,
            "reason": reason,
            "fields": fields[:2000] or "\n".join(item.get("raw_text", "") for item in decision_records)[:2000],
            "candidate_count": len(records),
            "candidate_records": decision_records,
            "selected_candidate_records": matched_records,
            "record_status_counts": {key: value for key, value in counts.items() if value},
            "selection_complete": True,
            "supported_capabilities": [item.value for item in capabilities],
            "required_capabilities": [item.value for item in capabilities],
            "missing_capabilities": [item.value for item in missing_capabilities],
        },
        request,
        domain="encounter",
        executor="numeric_ir",
    )


def execute_numeric_domain(
    request: DomainExecutionRequest,
    hints: str,
    *,
    fields: str = "",
) -> DomainExecutionResult | None:
    """Wrap the existing IR numeric engine in the unified domain contract."""
    if not request.numeric_execution_required:
        return None

    from microharness.medical.condition_execution import prejudge_numeric_hints

    domain = _numeric_domain(request)
    capabilities = _numeric_capabilities(domain)
    issue = request.numeric_comparison_issue()
    if issue:
        return normalize_domain_result(
            {
                "applicable": True,
                "status": EvidenceStatus.UNKNOWN.value,
                "reason_code": "INVALID_NUMERIC_IR",
                "data_quality": DataQuality.PARTIAL.value,
                "reason": issue,
                "fields": fields[:2000],
                "supported_capabilities": [item.value for item in capabilities],
                "required_capabilities": [item.value for item in capabilities],
                "missing_capabilities": [EvidenceCapability.NUMERIC_VALUE.value],
            },
            request,
            domain=domain,
            executor="numeric_ir",
        )

    record_decision = _execute_encounter_duration_records(
        request,
        capabilities=capabilities,
        fields=fields,
    )
    if record_decision is not None:
        return record_decision

    decision = prejudge_numeric_hints(request, hints)
    if decision is not None:
        matched = bool(decision.get("matched"))
        return normalize_domain_result(
            {
                "applicable": True,
                "matched": matched,
                "status": (
                    EvidenceStatus.MATCHED.value
                    if matched
                    else EvidenceStatus.NOT_MATCHED.value
                ),
                "reason_code": (
                    "NUMERIC_CONDITION_MET"
                    if matched
                    else "NUMERIC_CONDITION_NOT_MET"
                ),
                "data_quality": DataQuality.COMPLETE.value,
                "reason": str(decision.get("reason") or ""),
                "fields": fields[:2000],
                "supported_capabilities": [item.value for item in capabilities],
                "required_capabilities": [item.value for item in capabilities],
            },
            request,
            domain=domain,
            executor="numeric_ir",
        )

    reason = (
        "缺少明确年龄字段，无法判断年龄条件"
        if request.is_age_condition
        else "未取得与比较主体及单位一致的数值字段，无法判断数值条件"
    )
    return normalize_domain_result(
        {
            "applicable": True,
            "status": EvidenceStatus.UNKNOWN.value,
            "reason_code": "MISSING_NUMERIC_EVIDENCE",
            "data_quality": DataQuality.PARTIAL.value,
            "reason": reason,
            "fields": fields[:2000],
            "supported_capabilities": [item.value for item in capabilities],
            "required_capabilities": [item.value for item in capabilities],
            "missing_capabilities": [item.value for item in capabilities],
        },
        request,
        domain=domain,
        executor="numeric_ir",
    )


def _decision_trace_value(decision: Any, key: str) -> str:
    for item in reversed(tuple(getattr(decision, "trace", ()) or ())):
        if not isinstance(item, Mapping):
            continue
        value = str(item.get(key) or "").strip()
        if value:
            return value
    return ""


def _semantic_candidate_records(
    decisions: tuple[Any, ...],
    *,
    time_window: Any = None,
    record_time: datetime | None = None,
) -> tuple[dict[str, Any], ...]:
    """Convert validated semantic mentions into auditable record facts."""
    records: list[dict[str, Any]] = []
    window_required = bool(
        time_window is not None and getattr(time_window, "required", False)
    )
    for index, decision in enumerate(decisions, start=1):
        trace = tuple(getattr(decision, "trace", ()) or ())
        accepted = any(
            isinstance(item, Mapping)
            and item.get("stage") == "semantic_entity_recall"
            and item.get("accepted") is True
            for item in trace
        )
        if not accepted:
            continue
        status = str(getattr(decision, "status", "") or "").strip().upper()
        if status not in {
            EvidenceStatus.MATCHED.value,
            EvidenceStatus.NOT_MATCHED.value,
            EvidenceStatus.UNKNOWN.value,
        }:
            status = EvidenceStatus.UNKNOWN.value
        reason_code = str(
            getattr(decision, "reason_code", "") or ""
        ).strip().upper()
        if not window_required:
            scope_status = "NOT_REQUIRED"
        elif reason_code == "DOCUMENT_TIME_OUTSIDE_WINDOW":
            scope_status = "OUT_OF_SCOPE"
        elif reason_code in {
            "DOCUMENT_MENTION_TIME_UNKNOWN",
            "DOCUMENT_TIME_CONTEXT_INSUFFICIENT",
        }:
            scope_status = "UNKNOWN"
        else:
            scope_status = "IN_SCOPE"
        records.append({
            "record_index": index,
            "record_status": status,
            "record_reason_code": reason_code,
            "record_reason": str(getattr(decision, "reason", "") or ""),
            "scope_status": scope_status,
            "event_time": record_time.isoformat(sep=" ") if record_time else "",
            "matched_entity": _decision_trace_value(decision, "matched_entity"),
            "evidence_span": str(getattr(decision, "evidence", "") or ""),
        })
    return tuple(records)


def _finalize_document_decision(
    request: DomainExecutionRequest,
    text: str,
    decision: Any,
    *,
    matched_entity: str = "",
    candidate_decisions: tuple[Any, ...] = (),
    candidates_complete: bool = True,
    record_time: datetime | None = None,
    executor: str = "document_semantics",
) -> DomainExecutionResult | None:
    from microharness.medical.document_semantics import NO_DECISION
    from microharness.medical.semantic_rules import (
        judge_explicit_absence,
        judge_history_duration,
        judge_outcome_polarity,
    )
    if decision.status == NO_DECISION:
        return None

    semantic_type = resolve_condition_semantic_type(request)
    execution_entity = (
        matched_entity
        or _decision_trace_value(decision, "matched_entity")
        or request.entity
    )
    supported = [
        EvidenceCapability.ENTITY_PRESENCE,
        EvidenceCapability.DOCUMENT_CONTEXT,
        EvidenceCapability.SUBJECT_ATTRIBUTION,
    ]
    missing: list[EvidenceCapability] = []
    window_required = bool(
        request.time_window is not None
        and getattr(request.time_window, "required", False)
    )
    if window_required:
        supported.append(EvidenceCapability.TEMPORAL_OCCURRENCE)

    reason_code = decision.reason_code.strip().upper()
    if reason_code in {
        "DOCUMENT_MENTION_TIME_UNKNOWN",
        "DOCUMENT_TIME_CONTEXT_INSUFFICIENT",
    }:
        missing.append(EvidenceCapability.TEMPORAL_OCCURRENCE)
    if reason_code in {
        "DOCUMENT_COREFERENCE_UNRESOLVED",
        "DOCUMENT_SEMANTIC_CONFLICT",
        "DOCUMENT_SEMANTICS_INSUFFICIENT",
    }:
        missing.extend((
            EvidenceCapability.DOCUMENT_CONTEXT,
            EvidenceCapability.SUBJECT_ATTRIBUTION,
        ))

    raw = decision.to_dict()
    if semantic_type == ConditionSemanticType.HISTORY_PRESENCE:
        absence = judge_explicit_absence(execution_entity, text)
        if absence is not None:
            raw = absence
    elif semantic_type == ConditionSemanticType.HISTORY_DURATION:
        history = judge_history_duration(
            execution_entity,
            list(request.modifiers),
            text,
        )
        if history is not None:
            raw = history
            history_reason_code = str(history.get("reason_code") or "").upper()
            if history_reason_code in {
                "HISTORY_DURATION_MET",
                "HISTORY_DURATION_NOT_MET",
            }:
                supported.append(EvidenceCapability.HISTORY_DURATION)
            elif history_reason_code == "MISSING_HISTORY_DURATION":
                missing.append(EvidenceCapability.HISTORY_DURATION)
        elif decision.status == EvidenceStatus.MATCHED.value:
            raw = {
                "matched": False,
                "status": EvidenceStatus.UNKNOWN.value,
                "reason_code": "MISSING_HISTORY_DURATION",
                "reason": f"找到{execution_entity}相关病史，但未取得明确病史年限",
            }
            missing.append(EvidenceCapability.HISTORY_DURATION)
    elif semantic_type == ConditionSemanticType.OUTCOME_STATE:
        if decision.status not in {
            EvidenceStatus.NOT_MENTIONED.value,
            EvidenceStatus.UNKNOWN.value,
        }:
            outcome = judge_outcome_polarity(
                list(request.modifiers),
                text,
                expected_state=request.outcome_state,
            )
            if outcome is not None:
                raw = outcome
                supported.append(EvidenceCapability.OUTCOME_STATE)
            else:
                raw = {
                    "matched": False,
                    "status": EvidenceStatus.UNKNOWN.value,
                    "reason_code": "MISSING_OUTCOME_STATE",
                    "reason": (
                        f"病历提及{execution_entity}，但未提供所要求的阶段性转归状态证据"
                    ),
                }
                missing.append(EvidenceCapability.OUTCOME_STATE)

    semantic_records = _semantic_candidate_records(
        candidate_decisions,
        time_window=request.time_window,
        record_time=record_time,
    )
    quantifier_mode = str(request.quantifier.get("mode") or "").strip().lower()
    allow_document_quantifier = bool(
        semantic_records
        and quantifier_mode in {"any", "exists"}
        and semantic_type not in {
            ConditionSemanticType.HISTORY_DURATION,
            ConditionSemanticType.OUTCOME_STATE,
            ConditionSemanticType.NUMERIC_COMPARISON,
        }
    )
    if semantic_records:
        raw["candidate_records"] = [dict(item) for item in semantic_records]
        raw["candidate_count"] = len(semantic_records)
        raw["candidate_records_complete"] = bool(candidates_complete)
        raw["allow_document_quantifier"] = allow_document_quantifier
    if (
        semantic_records
        and request.quantifier
        and quantifier_mode not in {"", "any", "exists"}
    ):
        raw.update({
            "matched": False,
            "status": EvidenceStatus.UNKNOWN.value,
            "reason_code": "DOCUMENT_QUANTIFIER_RECORD_IDENTITY_UNAVAILABLE",
            "reason": (
                "病历语义召回得到的是文本提及，不具备可证明独立临床记录数量或顺序的记录标识，"
                "无法执行该量词条件"
            ),
        })

    final_status = str(raw.get("status") or "").strip().upper()
    final_reason_code = str(raw.get("reason_code") or "").strip().upper()
    uncertainty_kind = infer_evidence_uncertainty_kind(
        status=final_status,
        reason_code=final_reason_code,
        missing_capabilities=[item.value for item in missing],
        selection_complete=candidates_complete,
        semantic_trace=decision.trace,
        explicit=raw.get("uncertainty_kind"),
    )

    raw.update({
        "applicable": True,
        "data_quality": (
            DataQuality.PARTIAL.value
            if str(raw.get("status") or "").upper() == EvidenceStatus.UNKNOWN.value
            else DataQuality.COMPLETE.value
        ),
        "fields": text[:2000],
        "semantic_trace": list(decision.trace),
        "selection_complete": bool(candidates_complete),
        "candidate_records_complete": bool(candidates_complete),
        "matched_entity": execution_entity,
        "supported_capabilities": [item.value for item in supported],
        "missing_capabilities": [item.value for item in missing],
        "uncertainty_kind": uncertainty_kind.value,
    })
    return normalize_domain_result(
        raw,
        request,
        domain="document",
        executor=executor,
    )


def execute_document_domain(
    request: DomainExecutionRequest,
    text: str,
    *,
    record_time: datetime | None = None,
) -> DomainExecutionResult | None:
    """Run deterministic document semantics and expose its evidence contract."""
    from microharness.medical.document_semantics import assess_document_semantics

    decision = assess_document_semantics(
        request.entity,
        text,
        condition=request.condition,
        time_window=request.time_window,
        record_time=record_time,
    )
    return _finalize_document_decision(
        request,
        text,
        decision,
        matched_entity=request.entity,
        record_time=record_time,
    )


def execute_recalled_document_domain(
    request: DomainExecutionRequest,
    text: str,
    decision: Any,
    *,
    candidate_decisions: tuple[Any, ...] = (),
    candidates_complete: bool = True,
    record_time: datetime | None = None,
) -> DomainExecutionResult | None:
    """Adjudicate validated LLM-recalled mentions through the domain contract."""
    return _finalize_document_decision(
        request,
        text,
        decision,
        candidate_decisions=candidate_decisions,
        candidates_complete=candidates_complete,
        record_time=record_time,
        executor="semantic_entity_recall",
    )
