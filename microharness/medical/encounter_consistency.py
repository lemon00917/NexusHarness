"""Cross-source encounter identity and period consistency checks.

Natural-language models may identify which evidence is relevant, but they must
not merge records from different encounters. This module builds a generic
encounter profile from source metadata and bindings, then applies deterministic
identity and time-period constraints before condition adjudication.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from enum import Enum
import re
from collections.abc import Mapping, Sequence
from typing import Any

from microharness.medical.time_window import parse_datetime_value


class EncounterFieldRole(str, Enum):
    PATIENT_ID = "patient_id"
    ENCOUNTER_ID = "encounter_id"
    ENCOUNTER_START = "encounter_start"
    ENCOUNTER_END = "encounter_end"


class EncounterConsistencyStatus(str, Enum):
    CONSISTENT = "CONSISTENT"
    MINOR_VARIANCE = "MINOR_VARIANCE"
    INDEPENDENT_AGREEMENT = "INDEPENDENT_AGREEMENT"
    ENCOUNTER_CONFLICT = "ENCOUNTER_CONFLICT"
    INSUFFICIENT_IDENTITY = "INSUFFICIENT_IDENTITY"


@dataclass(frozen=True)
class SourceEncounterProfile:
    source_id: str
    source_name: str
    source_kind: str
    record_key: str
    patient_id: str = ""
    encounter_id: str = ""
    encounter_start: datetime | None = None
    encounter_end: datetime | None = None
    conclusion: str = ""
    completeness: str = "MISSING"
    provenance: Mapping[str, Any] = field(default_factory=dict)

    @property
    def has_complete_period(self) -> bool:
        return self.encounter_start is not None and self.encounter_end is not None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["encounter_start"] = _format_datetime(self.encounter_start)
        data["encounter_end"] = _format_datetime(self.encounter_end)
        data["has_complete_period"] = self.has_complete_period
        return data


@dataclass(frozen=True)
class EncounterConsistencyAssessment:
    status: EncounterConsistencyStatus
    blocks_adjudication: bool
    reason: str
    reason_code: str
    profiles: tuple[SourceEncounterProfile, ...] = ()
    conflicts: tuple[Mapping[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "blocks_adjudication": self.blocks_adjudication,
            "reason": self.reason,
            "reason_code": self.reason_code,
            "profiles": [item.to_dict() for item in self.profiles],
            "conflicts": [dict(item) for item in self.conflicts],
        }


_PERIOD_CAPABILITY = "ENCOUNTER_PERIOD"
_MINOR_VARIANCE = timedelta(hours=24)


_COMPATIBILITY_ALIASES: dict[EncounterFieldRole, tuple[str, ...]] = {
    EncounterFieldRole.PATIENT_ID: (
        "patientid", "patientkey", "globalpatientid", "hdcpatientid",
        "\u60a3\u8005id", "\u60a3\u8005\u7f16\u53f7",
        "\u75c5\u4ebaid", "\u75c5\u4eba\u7f16\u53f7",
    ),
    EncounterFieldRole.ENCOUNTER_ID: (
        "encounterid", "encounterkey", "visitid", "visitkey", "visitno",
        "visitnumber", "globalvisitid", "hdcencid", "encid", "\u5c31\u8bcaid",
        "\u5c31\u8bca\u7f16\u53f7", "\u4f4f\u9662\u53f7",
        "\u5c31\u8bca\u6d41\u6c34\u53f7",
    ),
    EncounterFieldRole.ENCOUNTER_START: (
        "encstart", "encstartdate", "encstarttime", "admissiondate",
        "admissiontime", "admissiondatetime", "encounterstart",
        "\u5165\u9662\u65e5\u671f", "\u5165\u9662\u65f6\u95f4",
        "\u5165\u9662\u65e5\u671f\u65f6\u95f4", "\u4f4f\u9662\u5f00\u59cb\u65f6\u95f4",
    ),
    EncounterFieldRole.ENCOUNTER_END: (
        "encend", "encenddate", "encendtime", "dischargedate",
        "dischargetime", "dischargedatetime", "encounterend",
        "\u51fa\u9662\u65e5\u671f", "\u51fa\u9662\u65f6\u95f4",
        "\u51fa\u9662\u65e5\u671f\u65f6\u95f4", "\u4f4f\u9662\u7ed3\u675f\u65f6\u95f4",
    ),
}


def _format_datetime(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S") if value else ""


def _normalize_token(value: Any) -> str:
    return re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", str(value or "")).lower()


def _as_values(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    if isinstance(value, (list, tuple, set)):
        return tuple(str(item) for item in value if str(item).strip())
    return ()


def _role_from_value(value: Any) -> EncounterFieldRole | None:
    normalized = _normalize_token(value)
    for role in EncounterFieldRole:
        if normalized == _normalize_token(role.value):
            return role
    return None


def _declared_field_roles(source: Mapping[str, Any]) -> dict[str, EncounterFieldRole]:
    """Return normalized field-token to canonical-role mappings."""
    declared: dict[str, EncounterFieldRole] = {}
    semantic = source.get("semantic") if isinstance(source.get("semantic"), Mapping) else {}
    containers = (
        semantic.get("field_roles"),
        semantic.get("semantic_fields"),
        source.get("field_roles"),
        source.get("semantic_fields"),
    )
    for container in containers:
        if not isinstance(container, Mapping):
            continue
        for key, raw_value in container.items():
            key_role = _role_from_value(key)
            value_role = _role_from_value(raw_value) if isinstance(raw_value, str) else None
            if key_role:
                for alias in _as_values(raw_value):
                    normalized = _normalize_token(alias)
                    if normalized:
                        declared[normalized] = key_role
            elif value_role:
                normalized = _normalize_token(key)
                if normalized:
                    declared[normalized] = value_role
    return declared


def _binding_tokens(binding: Mapping[str, Any]) -> tuple[str, ...]:
    path = str(binding.get("xml_path") or "")
    path_leaf = path.rsplit("/", 1)[-1]
    return tuple(
        value for value in (
            str(binding.get("html_field") or ""),
            str(binding.get("eng_field") or ""),
            str(binding.get("field_name") or binding.get("fieldName") or ""),
            path,
            path_leaf,
        ) if value.strip()
    )


def _binding_role(
    binding: Mapping[str, Any],
    declared: Mapping[str, EncounterFieldRole],
) -> EncounterFieldRole | None:
    for key in ("semantic_role", "field_role", "canonical_role", "role"):
        explicit = _role_from_value(binding.get(key))
        if explicit:
            return explicit
    normalized_tokens = tuple(_normalize_token(item) for item in _binding_tokens(binding))
    for token in normalized_tokens:
        if token in declared:
            return declared[token]
    for role, aliases in _COMPATIBILITY_ALIASES.items():
        normalized_aliases = tuple(_normalize_token(alias) for alias in aliases)
        for token in normalized_tokens:
            if any(alias and (token == alias or token.endswith(alias)) for alias in normalized_aliases):
                return role
    return None


def _record_key(binding: Mapping[str, Any]) -> str:
    for key in ("record_id", "recordId", "id"):
        value = str(binding.get(key) or "").strip()
        if value:
            return value
    label = str(binding.get("html_field") or "").strip()
    matched = re.match(r"^\[([^\]]+)\]", label)
    if matched:
        return matched.group(1).strip()
    return "record1"


def _binding_value(binding: Mapping[str, Any]) -> str:
    return str(binding.get("html_value") or binding.get("value") or "").strip()


def _parse_datetime_parts(values: Sequence[str]) -> datetime | None:
    clean = [str(value).strip() for value in values if str(value).strip()]
    if not clean:
        return None
    combined = parse_datetime_value(" ".join(clean))
    if combined:
        return combined
    for value in clean:
        parsed = parse_datetime_value(value)
        if parsed:
            return parsed
    return None


def _source_identity(source: Mapping[str, Any]) -> tuple[str, str, str]:
    source_name = str(source.get("file") or source.get("source_name") or "").strip()
    source_id = str(
        source.get("logical_source_id")
        or source.get("service_id")
        or source.get("source_id")
        or source.get("template")
        or source_name
    ).strip()
    source_kind = str(source.get("source_kind") or source.get("source_type") or "").strip()
    if not source_kind:
        source_kind = "service" if source.get("service_id") else "document"
    return source_id, source_name or source_id, source_kind


def _profile_completeness(values: Mapping[EncounterFieldRole, Sequence[str]]) -> str:
    has_start = bool(values.get(EncounterFieldRole.ENCOUNTER_START))
    has_end = bool(values.get(EncounterFieldRole.ENCOUNTER_END))
    if has_start and has_end:
        return "COMPLETE"
    if has_start or has_end:
        return "PARTIAL"
    return "MISSING"


def build_source_encounter_profiles(
    source: Mapping[str, Any],
    *,
    conclusion: str = "",
) -> tuple[SourceEncounterProfile, ...]:
    """Build encounter profiles without borrowing fields across source records."""
    if not isinstance(source, Mapping):
        return ()
    bindings = tuple(
        item for item in (source.get("bindings") or ())
        if isinstance(item, Mapping) and _binding_value(item)
    )
    if not bindings:
        return ()

    declared = _declared_field_roles(source)
    grouped: dict[str, dict[EncounterFieldRole, list[str]]] = {}
    provenance: dict[str, dict[str, list[dict[str, str]]]] = {}
    record_identities: dict[str, str] = {}
    for binding in bindings:
        role = _binding_role(binding, declared)
        if role is None:
            continue
        record_key = _record_key(binding)
        record_identity = str(binding.get("record_id") or binding.get("recordId") or "").strip()
        if record_identity:
            record_identities.setdefault(record_key, record_identity)
        value = _binding_value(binding)
        grouped.setdefault(record_key, {}).setdefault(role, []).append(value)
        provenance.setdefault(record_key, {}).setdefault(role.value, []).append({
            "label": str(binding.get("html_field") or ""),
            "field": str(binding.get("eng_field") or binding.get("fieldName") or ""),
            "path": str(binding.get("xml_path") or ""),
            "value": value,
        })

    source_id, source_name, source_kind = _source_identity(source)
    profiles: list[SourceEncounterProfile] = []
    for record_key, role_values in grouped.items():
        start = _parse_datetime_parts(role_values.get(EncounterFieldRole.ENCOUNTER_START, ()))
        end = _parse_datetime_parts(role_values.get(EncounterFieldRole.ENCOUNTER_END, ()))
        patient_values = role_values.get(EncounterFieldRole.PATIENT_ID, ())
        encounter_values = role_values.get(EncounterFieldRole.ENCOUNTER_ID, ())
        encounter_id = str(encounter_values[0]).strip() if encounter_values else record_identities.get(record_key, "")
        profiles.append(SourceEncounterProfile(
            source_id=source_id,
            source_name=source_name,
            source_kind=source_kind,
            record_key=record_key,
            patient_id=str(patient_values[0]).strip() if patient_values else "",
            encounter_id=encounter_id,
            encounter_start=start,
            encounter_end=end,
            conclusion=str(conclusion or ""),
            completeness=_profile_completeness(role_values),
            provenance={"field_roles": provenance.get(record_key, {})},
        ))
    return tuple(profiles)


def requires_encounter_consistency(file_results: Sequence[Mapping[str, Any]]) -> bool:
    """Activate only when execution explicitly requires an encounter period."""
    for result in file_results or ():
        if not isinstance(result, Mapping):
            continue
        capabilities = result.get("required_capabilities") or ()
        if isinstance(capabilities, str):
            capabilities = (capabilities,)
        normalized = {
            str(getattr(item, "value", item) or "").strip().upper()
            for item in capabilities
        }
        if _PERIOD_CAPABILITY in normalized:
            return True
    return False


def _profile_fingerprint(profile: SourceEncounterProfile) -> tuple[str, str, str, str]:
    return (
        _normalize_token(profile.patient_id),
        _normalize_token(profile.encounter_id),
        _format_datetime(profile.encounter_start),
        _format_datetime(profile.encounter_end),
    )


def _deduplicate_profiles(
    profiles: Sequence[SourceEncounterProfile],
) -> tuple[SourceEncounterProfile, ...]:
    unique: list[SourceEncounterProfile] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    for profile in profiles:
        key = (profile.source_id, *_profile_fingerprint(profile))
        if key in seen:
            continue
        seen.add(key)
        unique.append(profile)
    return tuple(unique)


def _profile_label(profile: SourceEncounterProfile) -> str:
    period = (
        f"{_format_datetime(profile.encounter_start)} "
        f"\u81f3 {_format_datetime(profile.encounter_end)}"
    )
    return f"{profile.source_name}[{profile.record_key}]({period})"


def _conflict(
    kind: str,
    left: SourceEncounterProfile,
    right: SourceEncounterProfile,
    detail: str,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "left_source": left.source_name,
        "left_record": left.record_key,
        "right_source": right.source_name,
        "right_record": right.record_key,
        "detail": detail,
    }


def _period_relation(
    left: SourceEncounterProfile,
    right: SourceEncounterProfile,
) -> tuple[str, str]:
    assert left.encounter_start and left.encounter_end
    assert right.encounter_start and right.encounter_end
    if left.encounter_end < left.encounter_start or right.encounter_end < right.encounter_start:
        return "conflict", "\u6765\u6e90\u5185\u7684\u5c31\u8bca\u7ed3\u675f\u65f6\u95f4\u65e9\u4e8e\u5f00\u59cb\u65f6\u95f4"

    start_delta = abs(left.encounter_start - right.encounter_start)
    end_delta = abs(left.encounter_end - right.encounter_end)
    if start_delta == timedelta(0) and end_delta == timedelta(0):
        return "consistent", "\u8d77\u6b62\u65f6\u95f4\u4e00\u81f4"
    if start_delta <= _MINOR_VARIANCE and end_delta <= _MINOR_VARIANCE:
        return "minor", "\u8d77\u6b62\u65f6\u95f4\u5b58\u5728\u4e0d\u8d85\u8fc724\u5c0f\u65f6\u7684\u6765\u6e90\u5dee\u5f02"

    overlap_start = max(left.encounter_start, right.encounter_start)
    overlap_end = min(left.encounter_end, right.encounter_end)
    if overlap_start <= overlap_end:
        return "minor", "\u5c31\u8bca\u65f6\u95f4\u6bb5\u6709\u91cd\u53e0\uff0c\u4f46\u6765\u6e90\u8bb0\u5f55\u7684\u8fb9\u754c\u65f6\u95f4\u5b58\u5728\u5dee\u5f02"

    gap = min(
        abs(left.encounter_start - right.encounter_end),
        abs(right.encounter_start - left.encounter_end),
    )
    if gap <= _MINOR_VARIANCE:
        return "minor", "\u5c31\u8bca\u65f6\u95f4\u6bb5\u76f8\u90bb\uff0c\u6765\u6e90\u65f6\u95f4\u5dee\u4e0d\u8d85\u8fc724\u5c0f\u65f6"
    return "conflict", "\u4e24\u4e2a\u6765\u6e90\u7684\u5b8c\u6574\u5c31\u8bca\u65f6\u95f4\u6bb5\u660e\u663e\u5206\u79bb"


def _result_conclusion(
    source: Mapping[str, Any],
    file_results: Sequence[Mapping[str, Any]],
) -> str:
    source_id, source_name, _ = _source_identity(source)
    for result in file_results or ():
        if not isinstance(result, Mapping):
            continue
        identifiers = {
            str(result.get("file") or "").strip(),
            str(result.get("logical_source_id") or "").strip(),
            str(result.get("service_id") or "").strip(),
            str(result.get("source_id") or "").strip(),
        }
        if source_name in identifiers or source_id in identifiers:
            return str(result.get("status") or ("MATCHED" if result.get("matched") else "")).strip()
    return ""


def _matching_results(
    source: Mapping[str, Any],
    file_results: Sequence[Mapping[str, Any]],
) -> tuple[Mapping[str, Any], ...]:
    source_id, source_name, _ = _source_identity(source)
    matched: list[Mapping[str, Any]] = []
    for result in file_results or ():
        if not isinstance(result, Mapping):
            continue
        identifiers = {
            str(result.get("file") or "").strip(),
            str(result.get("logical_source_id") or "").strip(),
            str(result.get("service_id") or "").strip(),
            str(result.get("source_id") or "").strip(),
        }
        if source_name in identifiers or source_id in identifiers:
            matched.append(result)
    return tuple(matched)


def _result_participates_in_consistency(result: Mapping[str, Any]) -> bool:
    """Return whether a source result can safely anchor encounter consistency.

    Encounter consistency protects against merging fields across visits.  A source
    that was already marked non-decisive for the current condition because it is
    missing required encounter-period capability should not create a blocking
    cross-source conflict against a decisive structured source.
    """
    status = str(result.get("status") or "").strip().upper()
    missing = result.get("missing_capabilities") or ()
    if isinstance(missing, str):
        missing = (missing,)
    has_missing_capability = any(
        str(getattr(item, "value", item) or "").strip()
        for item in missing
    )
    if status == "UNKNOWN" and has_missing_capability:
        return False
    if status == "UNKNOWN" and result.get("source_role_decisive") is False:
        return False
    return True


def _source_participates_in_consistency(
    source: Mapping[str, Any],
    file_results: Sequence[Mapping[str, Any]],
) -> bool:
    matching = _matching_results(source, file_results)
    if not matching:
        return True
    return any(_result_participates_in_consistency(result) for result in matching)


def _selected_record_tokens(
    source: Mapping[str, Any],
    file_results: Sequence[Mapping[str, Any]],
) -> set[str]:
    tokens: set[str] = set()
    for result in _matching_results(source, file_results):
        records = result.get("\u5019\u9009\u8bb0\u5f55") or result.get("candidate_records") or ()
        if not isinstance(records, Sequence) or isinstance(records, (str, bytes)):
            continue
        for record in records:
            if not isinstance(record, Mapping):
                continue
            metadata = record.get("metadata") if isinstance(record.get("metadata"), Mapping) else {}
            for value in (
                record.get("record_id"),
                record.get("record_key"),
                metadata.get("record_key"),
                metadata.get("internal_record_id"),
            ):
                normalized = _normalize_token(value)
                if normalized:
                    tokens.add(normalized)
    return tokens


def _ambiguous_source_conflicts(
    profiles: Sequence[SourceEncounterProfile],
) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    by_source: dict[str, list[SourceEncounterProfile]] = {}
    for profile in profiles:
        if profile.has_complete_period:
            by_source.setdefault(profile.source_id, []).append(profile)
    for source_profiles in by_source.values():
        if len(source_profiles) < 2:
            continue
        encounter_ids = {
            _normalize_token(item.encounter_id)
            for item in source_profiles if item.encounter_id
        }
        if len(encounter_ids) == 1 and all(item.encounter_id for item in source_profiles):
            continue
        left, right = source_profiles[0], source_profiles[1]
        conflicts.append(_conflict(
            "AMBIGUOUS_SOURCE_ENCOUNTERS",
            left,
            right,
            "\u540c\u4e00\u6765\u6e90\u8fd4\u56de\u591a\u4e2a\u5b8c\u6574\u5c31\u8bca\u5468\u671f\uff0c\u4f46\u7f3a\u5c11\u53ef\u552f\u4e00\u9009\u62e9\u5f53\u524d\u5c31\u8bca\u7684\u8eab\u4efd\u4fe1\u606f",
        ))
    return conflicts


def _normalized_conclusion(value: Any) -> str:
    normalized = _normalize_token(value).upper()
    if normalized in {"MATCHED", "TRUE", "符合", "匹配"}:
        return "MATCHED"
    if normalized in {"NOTMATCHED", "FALSE", "不符合", "不匹配"}:
        return "NOT_MATCHED"
    return ""


def _independent_conclusions_agree(
    profiles: Sequence[SourceEncounterProfile],
    conflicts: Sequence[Mapping[str, Any]],
) -> bool:
    """Allow separated anonymous periods only when no cross-source merge is needed."""
    if not conflicts or any(item.get("kind") != "PERIOD_SEPARATION" for item in conflicts):
        return False
    conclusions = [_normalized_conclusion(item.conclusion) for item in profiles]
    return bool(conclusions) and all(conclusions) and len(set(conclusions)) == 1


def assess_encounter_consistency(
    file_results: Sequence[Mapping[str, Any]],
    sources: Sequence[Mapping[str, Any]],
) -> EncounterConsistencyAssessment:
    """Assess whether encounter-period evidence belongs to one encounter.

    The assessment is intentionally independent of document names and Skill IDs.
    A future source participates through canonical semantic field-role declarations
    or the compatibility field vocabulary above.
    """
    raw_profiles: list[SourceEncounterProfile] = []
    for source in (sources or ()):
        if not isinstance(source, Mapping):
            continue
        if not _source_participates_in_consistency(source, file_results):
            continue
        source_profiles = build_source_encounter_profiles(
            source,
            conclusion=_result_conclusion(source, file_results),
        )
        selected_tokens = _selected_record_tokens(source, file_results)
        if selected_tokens:
            source_profiles = tuple(
                profile for profile in source_profiles
                if _normalize_token(profile.record_key) in selected_tokens
                or _normalize_token(profile.encounter_id) in selected_tokens
            )
        raw_profiles.extend(source_profiles)
    profiles = _deduplicate_profiles(tuple(raw_profiles))
    complete = tuple(item for item in profiles if item.has_complete_period)
    if len(complete) < 2:
        reason = (
            "\u4ec5\u4e00\u4e2a\u6765\u6e90\u63d0\u4f9b\u5b8c\u6574\u5c31\u8bca\u8d77\u6b62\u65f6\u95f4\uff0c\u65e0\u6cd5\u8fdb\u884c\u8de8\u6765\u6e90\u5c31\u8bca\u8eab\u4efd\u6838\u9a8c"
            if complete
            else "\u672a\u63d0\u53d6\u5230\u53ef\u7528\u4e8e\u8de8\u6765\u6e90\u6838\u9a8c\u7684\u5b8c\u6574\u5c31\u8bca\u8d77\u6b62\u65f6\u95f4"
        )
        return EncounterConsistencyAssessment(
            status=EncounterConsistencyStatus.INSUFFICIENT_IDENTITY,
            blocks_adjudication=False,
            reason=reason,
            reason_code="ENCOUNTER_IDENTITY_INSUFFICIENT",
            profiles=profiles,
        )

    conflicts = _ambiguous_source_conflicts(complete)
    minor_reasons: list[str] = []
    for index, left in enumerate(complete):
        for right in complete[index + 1:]:
            if left.source_id == right.source_id and _profile_fingerprint(left) == _profile_fingerprint(right):
                continue
            left_patient = _normalize_token(left.patient_id)
            right_patient = _normalize_token(right.patient_id)
            if left_patient and right_patient and left_patient != right_patient:
                conflicts.append(_conflict(
                    "PATIENT_ID_MISMATCH",
                    left,
                    right,
                    f"\u60a3\u8005\u6807\u8bc6\u4e0d\u4e00\u81f4\uff1a{left.patient_id} != {right.patient_id}",
                ))
                continue
            left_encounter = _normalize_token(left.encounter_id)
            right_encounter = _normalize_token(right.encounter_id)
            if left_encounter and right_encounter and left_encounter != right_encounter:
                conflicts.append(_conflict(
                    "ENCOUNTER_ID_MISMATCH",
                    left,
                    right,
                    f"\u5c31\u8bca\u6807\u8bc6\u4e0d\u4e00\u81f4\uff1a{left.encounter_id} != {right.encounter_id}",
                ))
                continue
            relation, detail = _period_relation(left, right)
            if relation == "conflict":
                conflicts.append(_conflict("PERIOD_SEPARATION", left, right, detail))
            elif relation == "minor":
                minor_reasons.append(
                    f"{left.source_name} \u4e0e {right.source_name}\uff1a{detail}"
                )

    if conflicts:
        if _independent_conclusions_agree(complete, conflicts):
            return EncounterConsistencyAssessment(
                status=EncounterConsistencyStatus.INDEPENDENT_AGREEMENT,
                blocks_adjudication=False,
                reason=(
                    "来源记录的完整就诊周期明显分离，未合并跨就诊字段；"
                    "但各来源均可独立完成条件判断且确定结论一致，因此不阻断本条件判定"
                ),
                reason_code="ENCOUNTER_CONTEXT_INDEPENDENT_AGREEMENT",
                profiles=profiles,
                conflicts=tuple(conflicts),
            )
        first = conflicts[0]
        reason = (
            "\u8de8\u6765\u6e90\u5c31\u8bca\u4e0a\u4e0b\u6587\u51b2\u7a81\uff0c\u4e0d\u80fd\u5c06\u8fd9\u4e9b\u8bc1\u636e\u5408\u5e76\u5224\u5b9a\u3002"
            f"{first['left_source']} \u4e0e {first['right_source']}\uff1a{first['detail']}"
        )
        return EncounterConsistencyAssessment(
            status=EncounterConsistencyStatus.ENCOUNTER_CONFLICT,
            blocks_adjudication=True,
            reason=reason,
            reason_code="ENCOUNTER_CONTEXT_CONFLICT",
            profiles=profiles,
            conflicts=tuple(conflicts),
        )

    if minor_reasons:
        return EncounterConsistencyAssessment(
            status=EncounterConsistencyStatus.MINOR_VARIANCE,
            blocks_adjudication=False,
            reason="\uff1b".join(dict.fromkeys(minor_reasons))[:500],
            reason_code="ENCOUNTER_CONTEXT_MINOR_VARIANCE",
            profiles=profiles,
        )

    labels = "\u3001".join(_profile_label(item) for item in complete[:4])
    return EncounterConsistencyAssessment(
        status=EncounterConsistencyStatus.CONSISTENT,
        blocks_adjudication=False,
        reason=f"\u8de8\u6765\u6e90\u5c31\u8bca\u8d77\u6b62\u65f6\u95f4\u4e00\u81f4\uff1a{labels}",
        reason_code="ENCOUNTER_CONTEXT_CONSISTENT",
        profiles=profiles,
    )
