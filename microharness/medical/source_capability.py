"""Generic source capability profiles for medical evidence.

The execution pipeline should not need to know individual skill ids. This
module builds a normalized profile from three inputs instead:

* the condition/source domain defaults,
* semantic declarations attached to a skill or IR node,
* explicit capabilities returned by a domain executor.

Callers may still apply clinical rules before building the profile; the output
is intentionally plain strings so it can be consumed without importing the
domain execution enums and creating a circular dependency.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence


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


_KNOWN_CAPABILITIES = {
    ENTITY_PRESENCE,
    TEMPORAL_OCCURRENCE,
    NUMERIC_VALUE,
    ABNORMALITY,
    REFERENCE_RANGE,
    ORDER_EVENT,
    ADMINISTRATION_EVENT,
    STATUS_VALIDITY,
    DIAGNOSIS_ASSERTION,
    OUTCOME_STATE,
    HISTORY_DURATION,
    SUBJECT_ATTRIBUTION,
    ENCOUNTER_PERIOD,
    DOCUMENT_CONTEXT,
}


_CAPABILITY_ALIASES = {
    "ENTITY": ENTITY_PRESENCE,
    "ENTITY_EXISTS": ENTITY_PRESENCE,
    "ENTITY_PRESENCE": ENTITY_PRESENCE,
    "FINDING": ENTITY_PRESENCE,
    "MENTION": ENTITY_PRESENCE,
    "MATCH": ENTITY_PRESENCE,
    "TEMPORAL": TEMPORAL_OCCURRENCE,
    "TIME": TEMPORAL_OCCURRENCE,
    "EVENT_TIME": TEMPORAL_OCCURRENCE,
    "PERFORMED_AT": TEMPORAL_OCCURRENCE,
    "ORDERED_AT": TEMPORAL_OCCURRENCE,
    "NUMERIC": NUMERIC_VALUE,
    "VALUE": NUMERIC_VALUE,
    "NUMERIC_VALUE": NUMERIC_VALUE,
    "ABNORMAL": ABNORMALITY,
    "ABNORMALITY": ABNORMALITY,
    "ABNORMAL_FLAG": ABNORMALITY,
    "REFERENCE": REFERENCE_RANGE,
    "REFERENCE_RANGE": REFERENCE_RANGE,
    "ORDER": ORDER_EVENT,
    "ORDERED": ORDER_EVENT,
    "ORDER_EVENT": ORDER_EVENT,
    "ADMINISTERED": ADMINISTRATION_EVENT,
    "ADMINISTRATION": ADMINISTRATION_EVENT,
    "ADMINISTRATION_EVENT": ADMINISTRATION_EVENT,
    "STATUS": STATUS_VALIDITY,
    "STATUS_VALIDITY": STATUS_VALIDITY,
    "DIAGNOSIS": DIAGNOSIS_ASSERTION,
    "DIAGNOSIS_ASSERTION": DIAGNOSIS_ASSERTION,
    "OUTCOME": OUTCOME_STATE,
    "OUTCOME_STATE": OUTCOME_STATE,
    "HISTORY_DURATION": HISTORY_DURATION,
    "DURATION": HISTORY_DURATION,
    "SUBJECT": SUBJECT_ATTRIBUTION,
    "SUBJECT_ATTRIBUTION": SUBJECT_ATTRIBUTION,
    "ENCOUNTER": ENCOUNTER_PERIOD,
    "ENCOUNTER_PERIOD": ENCOUNTER_PERIOD,
    "DOCUMENT": DOCUMENT_CONTEXT,
    "DOCUMENT_CONTEXT": DOCUMENT_CONTEXT,
}


_DOMAIN_DEFAULT_SUPPORTED = {
    "laboratory": (
        ENTITY_PRESENCE,
        TEMPORAL_OCCURRENCE,
        NUMERIC_VALUE,
        ABNORMALITY,
        REFERENCE_RANGE,
    ),
    "medication": (ENTITY_PRESENCE, ORDER_EVENT),
    "diagnosis": (ENTITY_PRESENCE, DIAGNOSIS_ASSERTION, TEMPORAL_OCCURRENCE),
    "document": (ENTITY_PRESENCE, DOCUMENT_CONTEXT, SUBJECT_ATTRIBUTION),
    "encounter": (ENCOUNTER_PERIOD, NUMERIC_VALUE, TEMPORAL_OCCURRENCE),
    "demographic": (NUMERIC_VALUE,),
    "numeric": (NUMERIC_VALUE,),
}


@dataclass(frozen=True)
class SourceCapabilityProfile:
    """Normalized capability profile for one source result."""

    domain: str
    semantic_type: str
    supported_capabilities: tuple[str, ...]
    required_capabilities: tuple[str, ...]
    missing_capabilities: tuple[str, ...]
    diagnostics: tuple[Mapping[str, Any], ...] = ()


def _unique(values: Sequence[str]) -> tuple[str, ...]:
    seen: list[str] = []
    for value in values:
        text = str(value or "").strip().upper()
        if text and text not in seen:
            seen.append(text)
    return tuple(seen)


def _capability_name(value: Any) -> str:
    raw = getattr(value, "value", value)
    text = str(raw or "").strip().upper()
    return _CAPABILITY_ALIASES.get(text, text if text in _KNOWN_CAPABILITIES else "")


def normalize_capabilities(values: Any) -> tuple[str, ...]:
    """Return known capability names from strings, enums, lists, or maps."""

    if values is None:
        return ()
    if isinstance(values, Mapping):
        normalized = []
        for key, enabled in values.items():
            if not enabled:
                continue
            name = _capability_name(key)
            if name:
                normalized.append(name)
        return _unique(normalized)
    if isinstance(values, str):
        parts = values.replace(";", ",").split(",")
        return _unique([name for part in parts if (name := _capability_name(part))])
    if isinstance(values, (list, tuple, set)):
        return _unique([name for item in values if (name := _capability_name(item))])
    name = _capability_name(values)
    return (name,) if name else ()


def _declared_capabilities(source: Mapping[str, Any], key: str) -> tuple[str, ...]:
    values: list[str] = []
    containers = [source]
    for container_key in (
        "capability_profile",
        "source_capability_profile",
        "evidence_contract",
    ):
        container = source.get(container_key)
        if isinstance(container, Mapping):
            containers.append(container)
    for container in containers:
        values.extend(normalize_capabilities(container.get(key)))
    return _unique(values)


def semantic_capability_enabled(
    semantic: Mapping[str, Any],
    name: str,
    default: bool = False,
) -> bool:
    """Check a semantic boolean capability without binding to a skill id."""

    if not isinstance(semantic, Mapping):
        return default
    keys = {str(name or "").strip(), _capability_name(name)}
    keys = {key for key in keys if key}
    for container_key in ("evidence_capabilities", "capabilities"):
        container = semantic.get(container_key)
        if not isinstance(container, Mapping):
            continue
        for key in keys:
            if key in container:
                return bool(container.get(key))
            lower_key = key.lower()
            if lower_key in container:
                return bool(container.get(lower_key))
        for capability_key, enabled in container.items():
            if _capability_name(capability_key) in keys:
                return bool(enabled)
    return default


def _semantic_supported_capabilities(semantic: Mapping[str, Any]) -> tuple[str, ...]:
    values = list(_declared_capabilities(semantic, "supported_capabilities"))
    for container_key in ("evidence_capabilities", "capabilities"):
        container = semantic.get(container_key)
        if isinstance(container, Mapping):
            values.extend(normalize_capabilities(container))
    return _unique(values)


def _has_discharge_diagnosis(candidate_records: Sequence[Mapping[str, Any]]) -> bool:
    for record in candidate_records:
        if not isinstance(record, Mapping):
            continue
        value = str(record.get("diagnosis_type") or record.get("type") or "").strip()
        local = str(record.get("\u8bca\u65ad\u7c7b\u578b") or "").strip()
        if value.lower() == "discharge" or "\u51fa\u9662" in local:
            return True
    return False


def build_source_capability_profile(
    *,
    domain: str,
    semantic_type: str,
    required_capabilities: Sequence[Any],
    semantic: Mapping[str, Any] | None = None,
    raw: Mapping[str, Any] | None = None,
    predicate: str = "",
    policy: Mapping[str, Any] | None = None,
    numeric_required: bool = False,
    has_numeric_comparison: bool = False,
    has_modifiers: bool = False,
    time_window_required: bool = False,
    diagnosis_phase_evidence_allowed: bool = False,
    candidate_records: Sequence[Mapping[str, Any]] = (),
    reason_code: str = "",
) -> SourceCapabilityProfile:
    """Build the capability contract for a source result."""

    source_domain = str(domain or "").strip().lower() or "document"
    semantic_name = str(semantic_type or "").strip().upper()
    semantic_map: Mapping[str, Any] = semantic if isinstance(semantic, Mapping) else {}
    raw_map: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
    policy_map: Mapping[str, Any] = policy if isinstance(policy, Mapping) else {}

    supported: list[str] = [ENTITY_PRESENCE]
    supported.extend(_DOMAIN_DEFAULT_SUPPORTED.get(source_domain, ()))
    required: list[str] = list(normalize_capabilities(required_capabilities))
    missing: list[str] = []
    diagnostics: list[Mapping[str, Any]] = []

    if source_domain == "laboratory":
        if numeric_required or has_numeric_comparison:
            required.append(NUMERIC_VALUE)
        elif has_modifiers:
            required.append(ABNORMALITY)
    elif source_domain == "medication":
        normalized_predicate = str(predicate or "").strip().lower()
        supported.extend(normalize_capabilities(policy_map.get("supported_capabilities")))
        required.extend(normalize_capabilities(policy_map.get("required_capabilities")))
        missing.extend(normalize_capabilities(policy_map.get("missing_capabilities")))
        if policy_map.get("required_status"):
            supported.append(STATUS_VALIDITY)
            required.append(STATUS_VALIDITY)
        if normalized_predicate == "administered":
            required.append(ADMINISTRATION_EVENT)
            if (
                semantic_capability_enabled(semantic_map, "administered", False)
                or bool(policy_map)
            ):
                supported.append(ADMINISTRATION_EVENT)
        else:
            required.append(ORDER_EVENT)
    elif source_domain == "diagnosis":
        if (
            semantic_name == OUTCOME_STATE
            and diagnosis_phase_evidence_allowed
            and _has_discharge_diagnosis(candidate_records)
        ):
            supported.append(OUTCOME_STATE)

    if time_window_required:
        required.append(TEMPORAL_OCCURRENCE)
        supported.append(TEMPORAL_OCCURRENCE)

    normalized_reason = str(reason_code or raw_map.get("reason_code") or "").strip().upper()
    if normalized_reason == "MISSING_EVENT_TIME":
        missing.append(TEMPORAL_OCCURRENCE)
    if (
        source_domain == "medication"
        and str(predicate or "").strip().lower() == "administered"
        and ADMINISTRATION_EVENT not in _unique(supported)
    ):
        missing.append(ADMINISTRATION_EVENT)
    if (
        source_domain == "medication"
        and normalized_reason == "INSUFFICIENT_EVIDENCE"
        and STATUS_VALIDITY in _unique(required)
    ):
        missing.append(STATUS_VALIDITY)

    semantic_supported = _semantic_supported_capabilities(semantic_map)
    semantic_required = _declared_capabilities(semantic_map, "required_capabilities")
    semantic_missing = _declared_capabilities(semantic_map, "missing_capabilities")
    raw_supported = _declared_capabilities(raw_map, "supported_capabilities")
    raw_required = _declared_capabilities(raw_map, "required_capabilities")
    raw_missing = _declared_capabilities(raw_map, "missing_capabilities")

    if semantic_supported:
        diagnostics.append({"source": "semantic", "supported": semantic_supported})
    if raw_supported:
        diagnostics.append({"source": "raw", "supported": raw_supported})

    supported.extend(semantic_supported)
    supported.extend(raw_supported)
    required.extend(semantic_required)
    required.extend(raw_required)
    missing.extend(semantic_missing)
    missing.extend(raw_missing)

    final_supported = _unique(supported)
    final_required = _unique(required)
    missing.extend(item for item in final_required if item not in final_supported)

    return SourceCapabilityProfile(
        domain=source_domain,
        semantic_type=semantic_name,
        supported_capabilities=final_supported,
        required_capabilities=final_required,
        missing_capabilities=_unique(missing),
        diagnostics=tuple(diagnostics),
    )
