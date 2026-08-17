'''Generic source-role and cross-source adjudication policy.'''

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence


PRIMARY = 'PRIMARY'
SUPPORTING = 'SUPPORTING'
CONTEXT = 'CONTEXT'
TIME_ANCHOR = 'TIME_ANCHOR'
CANDIDATE = 'CANDIDATE'

DECISIVE_ROLES = (PRIMARY, SUPPORTING, CANDIDATE)
NON_DECISIVE_ROLES = (CONTEXT, TIME_ANCHOR)
KNOWN_ROLES = DECISIVE_ROLES + NON_DECISIVE_ROLES


_ROLE_ALIASES = {
    'PRIMARY': PRIMARY,
    '\u4e3b\u8bc1\u636e': PRIMARY,
    'SUPPORTING': SUPPORTING,
    '\u8f85\u52a9\u8bc1\u636e': SUPPORTING,
    '\u8f85\u52a9\u4f9d\u636e': SUPPORTING,
    'CONTEXT': CONTEXT,
    '\u4e0a\u4e0b\u6587': CONTEXT,
    'TIME_ANCHOR': TIME_ANCHOR,
    'TEMPORAL_ANCHOR': TIME_ANCHOR,
    '\u65f6\u95f4\u8303\u56f4\u4f9d\u636e': TIME_ANCHOR,
    '\u4e8b\u4ef6\u951a\u70b9': TIME_ANCHOR,
    'CANDIDATE': CANDIDATE,
    '\u5019\u9009\u8bc1\u636e': CANDIDATE,
}


@dataclass(frozen=True)
class SourceRolePolicy:
    '''Resolved role and decision authority for one evidence source.'''

    role: str
    acceptable: bool
    decisive: bool
    policy_source: str
    rationale: str


@dataclass(frozen=True)
class CrossSourceDecision:
    '''Role-aware result of combining normalized source decisions.'''

    status: str
    conflict_level: str
    reason_code: str


def normalize_source_role(value: Any, default: str = CANDIDATE) -> str:
    text = str(getattr(value, 'value', value) or '').strip()
    return _ROLE_ALIASES.get(text, _ROLE_ALIASES.get(text.upper(), default))


def normalize_source_roles(values: Any) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, str):
        values = values.replace(';', ',').split(',')
    if isinstance(values, Mapping):
        values = [key for key, enabled in values.items() if enabled]
    if not isinstance(values, (list, tuple, set)):
        values = [values]
    roles = []
    for value in values:
        role = normalize_source_role(value, default='')
        if role and role not in roles:
            roles.append(role)
    return tuple(roles)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _declared_role(
    raw: Mapping[str, Any],
    semantic: Mapping[str, Any],
    semantic_type: str,
) -> tuple[str, str]:
    for source_name, source in (('result', raw), ('semantic', semantic)):
        direct = source.get('source_role') or source.get('evidence_role')
        if direct:
            return normalize_source_role(direct), f'{source_name}.source_role'

        capability_profile = _mapping(
            source.get('capability_profile')
            or source.get('source_capability_profile')
        )
        profile = _mapping(
            source.get('evidence_policy')
            or source.get('source_role_policy')
            or source.get('role_policy')
            or capability_profile.get('evidence_policy')
            or capability_profile.get('source_role_policy')
            or capability_profile.get('role_policy')
        )
        by_semantic_type = _mapping(
            profile.get('by_semantic_type') or profile.get('semantic_roles')
        )
        normalized_type = semantic_type.strip().upper()
        semantic_role = next((
            role for key, role in by_semantic_type.items()
            if str(key).strip().upper() == normalized_type
        ), None)
        if semantic_role:
            return normalize_source_role(semantic_role), f'{source_name}.role_policy'
        default_role = profile.get('default_role') or profile.get('default')
        if default_role:
            return normalize_source_role(default_role), f'{source_name}.role_policy'
    return '', ''


def resolve_source_role_policy(
    *,
    raw: Mapping[str, Any] | None = None,
    semantic: Mapping[str, Any] | None = None,
    semantic_type: str = '',
    source_kind: str = '',
    is_primary_source: bool = False,
    has_primary_source: bool = False,
    is_time_anchor: bool = False,
    is_routed: bool = False,
    acceptable_roles: Sequence[Any] = DECISIVE_ROLES,
    missing_capabilities: Sequence[Any] = (),
) -> SourceRolePolicy:
    '''Resolve role from metadata first, then generic execution context.'''

    raw = raw or {}
    semantic = semantic or {}
    role, policy_source = _declared_role(raw, semantic, semantic_type)
    if not role:
        if is_time_anchor:
            role, policy_source = TIME_ANCHOR, 'execution.time_anchor'
        elif source_kind == 'service':
            if is_primary_source or is_routed:
                role, policy_source = PRIMARY, 'execution.primary_service'
            elif has_primary_source:
                role, policy_source = SUPPORTING, 'execution.supporting_service'
            else:
                role, policy_source = CANDIDATE, 'execution.candidate_service'
        elif source_kind == 'document' and is_routed:
            role = SUPPORTING if has_primary_source else PRIMARY
            policy_source = 'execution.routed_document'
        else:
            role, policy_source = CANDIDATE, 'execution.candidate'

    allowed = normalize_source_roles(acceptable_roles) or DECISIVE_ROLES
    missing = tuple(dict.fromkeys(
        str(getattr(value, 'value', value) or '').strip().upper()
        for value in missing_capabilities
        if str(getattr(value, 'value', value) or '').strip()
    ))
    acceptable = role in allowed
    decisive = acceptable and role not in NON_DECISIVE_ROLES and not missing
    if missing:
        rationale = 'missing_required_capabilities'
    elif not acceptable:
        rationale = 'source_role_not_acceptable'
    elif role in NON_DECISIVE_ROLES:
        rationale = 'source_role_not_decisive'
    else:
        rationale = 'source_role_decisive'
    return SourceRolePolicy(role, acceptable, decisive, policy_source, rationale)


def _negative_reason_code(decisions: Sequence[Mapping[str, Any]]) -> str:
    codes = list(dict.fromkeys(
        str(item.get('reason_code') or '') for item in decisions
    ))
    codes = [code for code in codes if code]
    return codes[0] if len(codes) == 1 else 'NO_MATCHING_RECORD'


def adjudicate_source_decisions(
    decisions: Sequence[Mapping[str, Any]],
) -> CrossSourceDecision:
    '''Combine source decisions with the conservative four-state policy.'''

    voting = [
        item for item in decisions
        if normalize_source_role(item.get('source_role')) not in NON_DECISIVE_ROLES
    ]
    if not voting:
        return CrossSourceDecision('UNKNOWN', 'NONE', 'INSUFFICIENT_EVIDENCE')

    primary = [
        item for item in voting
        if normalize_source_role(item.get('source_role')) == PRIMARY
    ]
    supporting = [item for item in voting if item not in primary]
    if not primary:
        primary, supporting = supporting, []

    primary_statuses = {str(item.get('status') or 'UNKNOWN') for item in primary}
    supporting_statuses = {
        str(item.get('status') or 'UNKNOWN') for item in supporting
    }
    primary_has_match = 'MATCHED' in primary_statuses
    primary_has_negative = 'NOT_MATCHED' in primary_statuses
    primary_has_unknown = 'UNKNOWN' in primary_statuses

    if primary_has_match and primary_has_negative:
        return CrossSourceDecision(
            'UNKNOWN', 'CONCLUSIVE_CONFLICT', 'EVIDENCE_CONFLICT'
        )
    if primary_has_match:
        if 'NOT_MATCHED' in supporting_statuses:
            return CrossSourceDecision(
                'MATCHED', 'SUPPORTING_DISAGREEMENT', 'MATCH_CONFIRMED'
            )
        return CrossSourceDecision('MATCHED', 'NONE', 'MATCH_CONFIRMED')
    if primary_has_negative:
        if 'MATCHED' in supporting_statuses:
            return CrossSourceDecision(
                'UNKNOWN', 'CONCLUSIVE_CONFLICT', 'EVIDENCE_CONFLICT'
            )
        if primary_has_unknown or 'UNKNOWN' in supporting_statuses:
            return CrossSourceDecision('UNKNOWN', 'NONE', 'INSUFFICIENT_EVIDENCE')
        return CrossSourceDecision(
            'NOT_MATCHED', 'NONE', _negative_reason_code(primary)
        )

    if 'MATCHED' in supporting_statuses and 'NOT_MATCHED' in supporting_statuses:
        return CrossSourceDecision(
            'UNKNOWN', 'CONCLUSIVE_CONFLICT', 'EVIDENCE_CONFLICT'
        )
    if 'MATCHED' in supporting_statuses:
        return CrossSourceDecision('MATCHED', 'NONE', 'MATCH_CONFIRMED')
    if 'NOT_MATCHED' in supporting_statuses:
        if 'UNKNOWN' in supporting_statuses:
            return CrossSourceDecision('UNKNOWN', 'NONE', 'INSUFFICIENT_EVIDENCE')
        return CrossSourceDecision(
            'NOT_MATCHED', 'NONE', _negative_reason_code(supporting)
        )

    all_statuses = primary_statuses | supporting_statuses
    if 'UNKNOWN' not in all_statuses and 'NOT_MENTIONED' in all_statuses:
        return CrossSourceDecision('NOT_MENTIONED', 'NONE', 'NO_MATCHING_RECORD')

    unknown_decisions = [
        item for item in primary + supporting if item.get('status') == 'UNKNOWN'
    ]
    complete_not_mentioned = any(
        item.get('status') == 'NOT_MENTIONED'
        and item.get('data_quality') == 'COMPLETE'
        and item.get('selection_complete') is not False
        for item in primary + supporting
    )
    complete_primary_not_mentioned = any(
        item.get('status') == 'NOT_MENTIONED'
        and item.get('data_quality') == 'COMPLETE'
        and item.get('selection_complete') is not False
        and normalize_source_role(item.get('source_role')) == PRIMARY
        for item in primary + supporting
    )

    def _unknown_source_non_decisive(item: Mapping[str, Any]) -> bool:
        if (
            item.get('reason_code') == 'SOURCE_UNAVAILABLE'
            or item.get('data_quality') == 'SOURCE_ERROR'
            or item.get('uncertainty_kind') == 'REJECTED_CANDIDATE'
        ):
            return True
        return (
            complete_primary_not_mentioned
            and item.get('uncertainty_kind') in {
                'INCOMPLETE_SEARCH',
                'UNRESOLVED_CANDIDATE',
            }
            and normalize_source_role(item.get('source_role')) != PRIMARY
        )

    unknown_sources_non_decisive = bool(unknown_decisions) and all(
        _unknown_source_non_decisive(item)
        for item in unknown_decisions
    )
    if complete_not_mentioned and unknown_sources_non_decisive:
        return CrossSourceDecision('NOT_MENTIONED', 'NONE', 'NO_MATCHING_RECORD')

    if any(
        item.get('reason_code') == 'SOURCE_UNAVAILABLE'
        for item in primary + supporting
    ):
        reason_code = 'SOURCE_UNAVAILABLE'
    else:
        unknown_codes = {
            str(item.get('reason_code') or '')
            for item in primary + supporting
            if item.get('status') == 'UNKNOWN'
        }
        reason_code = next(
            (
                code for code in (
                    'MISSING_REQUIRED_CAPABILITY',
                    'SOURCE_ROLE_NOT_DECISIVE',
                    'INCOMPLETE_CANDIDATE_SET',
                )
                if code in unknown_codes
            ),
            'INSUFFICIENT_EVIDENCE',
        )
    return CrossSourceDecision('UNKNOWN', 'NONE', reason_code)
