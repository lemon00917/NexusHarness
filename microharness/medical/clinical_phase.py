'''Metadata-driven clinical phase inference for document evidence.'''

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Iterable, Mapping


CLINICAL_PHASE_PRIORITY = {
    'unknown': 0,
    'admission': 10,
    'hospitalization': 20,
    'post_treatment': 30,
    'postoperative': 30,
    'discharge': 40,
    'post_discharge': 50,
    'follow_up': 60,
}

_VALID_SOURCE_ROLES = {
    'PRIMARY',
    'SUPPORTING',
    'CONTEXT',
    'TIME_ANCHOR',
    'CANDIDATE',
}


@dataclass(frozen=True)
class SourcePhaseProfile:
    phases: tuple[str, ...] = ()
    section_phases: dict[str, str] = field(default_factory=dict)
    document_phase: str = ''
    inference_source: str = ''

    def to_dict(self) -> dict[str, Any]:
        return {
            'clinical_phases': list(self.phases),
            'section_phases': dict(self.section_phases),
            'document_phase': self.document_phase,
            'phase_inference_source': self.inference_source,
        }


def _text(value: object) -> str:
    if isinstance(value, (list, tuple, set)):
        return ' '.join(str(item or '') for item in value)
    return str(value or '')


def _normalize_name(value: object) -> str:
    return re.sub(r'\s+', '', str(value or '')).lower()


def normalize_clinical_phase(value: object) -> str:
    '''Normalize explicit phase fields and descriptive metadata.'''
    text = _text(value).strip()
    normalized = text.lower().replace('-', '_').replace(' ', '_')
    aliases = {
        'baseline': 'admission',
        'pre_treatment': 'admission',
        'inpatient': 'hospitalization',
        'treatment': 'hospitalization',
        'outcome': 'discharge',
        'post_op': 'postoperative',
        'followup': 'follow_up',
    }
    normalized = aliases.get(normalized, normalized)
    if normalized in CLINICAL_PHASE_PRIORITY:
        return normalized if normalized != 'unknown' else ''

    compact = re.sub(r'\s+', '', text).lower()
    patterns = (
        ('post_discharge', r'出院后|离院后|院外|post.?discharge'),
        ('follow_up', r'随访|复诊|复查|门诊随诊|follow.?up'),
        ('postoperative', r'术后|手术后|post.?operative|post.?surgery'),
        ('post_treatment', r'治疗后|用药后|处置后|干预后|post.?treatment'),
        ('admission', r'入院时|入院前|入院初|治疗前|初诊时|基线|发病经过|入院概况|admission|baseline'),
        (
            'hospitalization',
            r'住院期间|住院过程|(?<!外院)(?<!院外)诊疗经过|治疗过程|病程变化|住院动态|hospitalization|inpatient',
        ),
        ('discharge', r'出院时|离院时|出院转归|出院情况|最终转归|治疗结果|discharge|outcome'),
    )
    matched = [
        phase
        for phase, pattern in patterns
        if re.search(pattern, compact, re.I)
    ]
    if not matched:
        return ''
    return max(matched, key=lambda phase: CLINICAL_PHASE_PRIORITY.get(phase, 0))


def _explicit_phase(metadata: Mapping[str, Any] | None) -> str:
    if not isinstance(metadata, Mapping):
        return ''
    for key in ('clinical_phase', 'evidence_phase', 'outcome_phase', 'phase'):
        phase = normalize_clinical_phase(metadata.get(key))
        if phase:
            return phase
    semantic = metadata.get('semantic')
    if isinstance(semantic, Mapping):
        for key in ('clinical_phase', 'evidence_phase', 'outcome_phase', 'phase'):
            phase = normalize_clinical_phase(semantic.get(key))
            if phase:
                return phase
    return ''


def infer_metadata_phase(metadata: Mapping[str, Any] | None) -> str:
    '''Infer a phase from explicit fields first, then generic metadata text.'''
    explicit = _explicit_phase(metadata)
    if explicit:
        return explicit
    if not isinstance(metadata, Mapping):
        return ''
    descriptive_values = []
    for key in (
        'info_type',
        'purpose',
        'used_for',
        'document_role',
        'document_roles',
        'section_role',
        'section_roles',
    ):
        descriptive_values.append(metadata.get(key))
    return normalize_clinical_phase(descriptive_values)


def _section_items(document_metadata: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(document_metadata, Mapping):
        return []
    sections = document_metadata.get('sections')
    if isinstance(sections, list):
        return [dict(item) for item in sections if isinstance(item, Mapping)]
    return []


def _resolve_section_metadata(
    document_metadata: Mapping[str, Any] | None,
    section_name: str,
) -> dict[str, Any] | None:
    requested = _normalize_name(section_name)
    if not requested:
        return None
    for section in _section_items(document_metadata):
        aliases = section.get('aliases') or []
        names = [section.get('name'), *aliases]
        if requested in {_normalize_name(name) for name in names if name}:
            return section
    return None


def infer_document_source_phase(
    document_metadata: Mapping[str, Any] | None,
    section_names: Iterable[str] = (),
) -> SourcePhaseProfile:
    '''Infer phases from the sections actually fetched for one document.'''
    section_phases: dict[str, str] = {}
    requested_sections = [str(name).strip() for name in section_names if str(name).strip()]
    for section_name in requested_sections:
        section_metadata = _resolve_section_metadata(document_metadata, section_name)
        phase = infer_metadata_phase(section_metadata)
        if phase:
            section_phases[section_name] = phase

    document_phase = infer_metadata_phase(document_metadata)
    phases = list(dict.fromkeys(section_phases.values()))
    if not phases and document_phase:
        phases.append(document_phase)
    inference_source = (
        'section_metadata'
        if section_phases
        else 'document_metadata'
        if document_phase
        else ''
    )
    return SourcePhaseProfile(
        phases=tuple(phases),
        section_phases=section_phases,
        document_phase=document_phase,
        inference_source=inference_source,
    )


def resolve_outcome_target_phase(
    explicit_phase: object,
    source_profiles: Iterable[SourcePhaseProfile | Mapping[str, Any]],
) -> str:
    '''Resolve an explicit phase or the latest available non-baseline phase.'''
    normalized = normalize_clinical_phase(explicit_phase)
    if normalized:
        return normalized

    available: list[str] = []
    for profile in source_profiles:
        if isinstance(profile, SourcePhaseProfile):
            phases = profile.phases
        elif isinstance(profile, Mapping):
            phases = profile.get('clinical_phases') or profile.get('phases') or ()
        else:
            phases = ()
        for phase in phases:
            normalized_phase = normalize_clinical_phase(phase)
            if normalized_phase and normalized_phase != 'admission':
                available.append(normalized_phase)
    if not available:
        return ''
    return max(available, key=lambda phase: CLINICAL_PHASE_PRIORITY.get(phase, 0))


def source_supports_outcome_state(source: Mapping[str, Any] | None) -> bool:
    '''Read an outcome-state capability without depending on a Skill ID.'''
    if not isinstance(source, Mapping):
        return False
    semantic = source.get('semantic') if isinstance(source.get('semantic'), Mapping) else {}
    capability_values = []
    for container in (source, semantic):
        capabilities = container.get('evidence_capabilities') or container.get('capabilities')
        if isinstance(capabilities, Mapping):
            if any(
                bool(capabilities.get(key))
                for key in ('outcome', 'outcome_state', 'clinical_outcome')
            ):
                return True
            capability_values.extend(key for key, enabled in capabilities.items() if enabled)
        elif isinstance(capabilities, (list, tuple, set)):
            capability_values.extend(capabilities)
        evidence_types = container.get('evidence_types') or ()
        if isinstance(evidence_types, str):
            evidence_types = [evidence_types]
        capability_values.extend(evidence_types)
    normalized = {_normalize_name(value) for value in capability_values}
    return bool(
        normalized.intersection(
            {'outcome', 'outcomestate', 'clinicaloutcome', 'outcomeevidence'}
        )
    )


def classify_outcome_source_role(
    profile: SourcePhaseProfile | Mapping[str, Any] | None,
    *,
    target_phase: object,
    source_kind: str,
    supports_outcome_state: bool = False,
    explicit_role: object = '',
) -> str:
    '''Assign a voting role for an outcome condition from generic semantics.'''
    normalized_role = str(explicit_role or '').strip().upper()
    if normalized_role in _VALID_SOURCE_ROLES:
        return normalized_role
    if str(source_kind or '').strip().lower() == 'service':
        return 'PRIMARY' if supports_outcome_state else 'SUPPORTING'

    if isinstance(profile, SourcePhaseProfile):
        phases = profile.phases
    elif isinstance(profile, Mapping):
        phases = profile.get('clinical_phases') or profile.get('phases') or ()
    else:
        phases = ()
    normalized_phases = {
        normalize_clinical_phase(phase)
        for phase in phases
        if normalize_clinical_phase(phase)
    }
    normalized_target = normalize_clinical_phase(target_phase)
    if normalized_target and normalized_target in normalized_phases:
        return 'PRIMARY'
    return 'CONTEXT'
