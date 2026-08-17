from microharness.medical.evidence_policy import (
    CANDIDATE,
    PRIMARY,
    SUPPORTING,
    TIME_ANCHOR,
    adjudicate_source_decisions,
    resolve_source_role_policy,
)


def test_future_skill_declares_role_by_semantic_type_without_skill_id():
    policy = resolve_source_role_policy(
        semantic={
            'role_policy': {
                'by_semantic_type': {'OUTCOME_STATE': 'PRIMARY'},
                'default_role': 'SUPPORTING',
            }
        },
        semantic_type='OUTCOME_STATE',
        source_kind='service',
        acceptable_roles=[PRIMARY, SUPPORTING],
    )

    assert policy.role == PRIMARY
    assert policy.decisive is True
    assert policy.policy_source == 'semantic.role_policy'


def test_future_skill_can_nest_role_policy_in_capability_profile():
    policy = resolve_source_role_policy(
        raw={
            'capability_profile': {
                'role_policy': {
                    'by_semantic_type': {'numeric_comparison': 'SUPPORTING'}
                }
            }
        },
        semantic_type='NUMERIC_COMPARISON',
        acceptable_roles=[PRIMARY, SUPPORTING],
    )

    assert policy.role == SUPPORTING
    assert policy.decisive is True


def test_time_anchor_is_non_decisive_for_condition_result():
    policy = resolve_source_role_policy(
        source_kind='document',
        is_time_anchor=True,
        acceptable_roles=[PRIMARY, SUPPORTING, CANDIDATE],
    )

    assert policy.role == TIME_ANCHOR
    assert policy.acceptable is False
    assert policy.decisive is False


def test_missing_capability_prevents_role_from_being_decisive():
    policy = resolve_source_role_policy(
        raw={'source_role': 'PRIMARY'},
        acceptable_roles=[PRIMARY],
        missing_capabilities=['TEMPORAL_OCCURRENCE'],
    )

    assert policy.role == PRIMARY
    assert policy.acceptable is True
    assert policy.decisive is False
    assert policy.rationale == 'missing_required_capabilities'


def test_primary_conflict_remains_unknown():
    decision = adjudicate_source_decisions([
        {'source_role': PRIMARY, 'status': 'MATCHED'},
        {'source_role': PRIMARY, 'status': 'NOT_MATCHED'},
    ])

    assert decision.status == 'UNKNOWN'
    assert decision.conflict_level == 'CONCLUSIVE_CONFLICT'
    assert decision.reason_code == 'EVIDENCE_CONFLICT'


def test_primary_positive_keeps_result_when_supporting_negative():
    decision = adjudicate_source_decisions([
        {'source_role': PRIMARY, 'status': 'MATCHED'},
        {'source_role': SUPPORTING, 'status': 'NOT_MATCHED'},
    ])

    assert decision.status == 'MATCHED'
    assert decision.conflict_level == 'SUPPORTING_DISAGREEMENT'


def test_supporting_match_can_take_over_unavailable_primary():
    decision = adjudicate_source_decisions([
        {
            'source_role': PRIMARY,
            'status': 'UNKNOWN',
            'reason_code': 'SOURCE_UNAVAILABLE',
            'data_quality': 'SOURCE_ERROR',
        },
        {'source_role': SUPPORTING, 'status': 'MATCHED'},
    ])

    assert decision.status == 'MATCHED'
    assert decision.reason_code == 'MATCH_CONFIRMED'


def test_complete_not_mentioned_can_outweigh_rejected_candidate():
    decision = adjudicate_source_decisions([
        {
            'source_role': PRIMARY,
            'status': 'NOT_MENTIONED',
            'data_quality': 'COMPLETE',
            'selection_complete': True,
        },
        {
            'source_role': SUPPORTING,
            'status': 'UNKNOWN',
            'uncertainty_kind': 'REJECTED_CANDIDATE',
        },
    ])

    assert decision.status == 'NOT_MENTIONED'
    assert decision.reason_code == 'NO_MATCHING_RECORD'

def test_complete_primary_not_mentioned_outweighs_supporting_incomplete_search():
    decision = adjudicate_source_decisions([
        {
            'source_role': PRIMARY,
            'status': 'NOT_MENTIONED',
            'data_quality': 'COMPLETE',
            'selection_complete': True,
        },
        {
            'source_role': SUPPORTING,
            'status': 'UNKNOWN',
            'uncertainty_kind': 'INCOMPLETE_SEARCH',
        },
    ])

    assert decision.status == 'NOT_MENTIONED'
    assert decision.reason_code == 'NO_MATCHING_RECORD'


def test_complete_primary_not_mentioned_outweighs_supporting_unresolved_candidate():
    decision = adjudicate_source_decisions([
        {
            'source_role': PRIMARY,
            'status': 'NOT_MENTIONED',
            'data_quality': 'COMPLETE',
            'selection_complete': True,
        },
        {
            'source_role': SUPPORTING,
            'status': 'UNKNOWN',
            'uncertainty_kind': 'UNRESOLVED_CANDIDATE',
        },
    ])

    assert decision.status == 'NOT_MENTIONED'
    assert decision.reason_code == 'NO_MATCHING_RECORD'


def test_primary_incomplete_search_stays_unknown():
    decision = adjudicate_source_decisions([
        {
            'source_role': PRIMARY,
            'status': 'UNKNOWN',
            'uncertainty_kind': 'INCOMPLETE_SEARCH',
        },
        {
            'source_role': SUPPORTING,
            'status': 'NOT_MENTIONED',
            'data_quality': 'COMPLETE',
            'selection_complete': True,
        },
    ])

    assert decision.status == 'UNKNOWN'
    assert decision.reason_code == 'INSUFFICIENT_EVIDENCE'
