from microharness.medical.clinical_phase import (
    classify_outcome_source_role,
    infer_document_source_phase,
    resolve_outcome_target_phase,
    source_supports_outcome_state,
)
from microharness.medical.evidence import (
    ConflictLevel,
    EvidenceStatus,
    build_condition_result,
)
from microharness.medical.semantic_rules import augment_analysis_routes


def _document_metadata(section_name, info_type, purpose):
    return {
        'purpose': purpose,
        'sections': [
            {
                'name': section_name,
                'info_type': info_type,
                'purpose': purpose,
            }
        ],
    }


def _file_result(name, status, role):
    return {
        'file': name,
        'status': status,
        'matched': status == 'MATCHED',
        'source_role': role,
        'reason': f'{name}:{status}',
        'fields': name,
    }


def test_phase_inference_uses_metadata_instead_of_document_name():
    baseline = _document_metadata(
        'history',
        '入院概况',
        '记录入院时已经存在的症状和治疗前基线',
    )
    outcome = _document_metadata(
        'status',
        '出院转归',
        '记录出院时症状改善程度和治疗结果',
    )

    baseline_profile = infer_document_source_phase(baseline, ['history'])
    outcome_profile = infer_document_source_phase(outcome, ['status'])

    assert baseline_profile.phases == ('admission',)
    assert outcome_profile.phases == ('discharge',)


def test_actual_section_phase_overrides_broader_document_purpose():
    metadata = {
        'purpose': '出院时总结住院诊疗全过程和最终结果',
        'sections': [
            {'name': '入院情况', 'info_type': '入院概况'},
            {'name': '诊疗经过', 'info_type': '治疗过程'},
            {'name': '出院情况', 'info_type': '出院转归'},
        ],
    }

    baseline = infer_document_source_phase(metadata, ['入院情况'])
    mixed = infer_document_source_phase(metadata, ['诊疗经过', '出院情况'])

    assert baseline.phases == ('admission',)
    assert mixed.phases == ('hospitalization', 'discharge')


def test_external_treatment_history_in_admission_note_is_not_hospitalization_phase():
    metadata = {
        'purpose': '患者入院时建立的首份完整病历',
        'sections': [
            {
                'name': '现病史',
                'info_type': '发病经过',
                'purpose': '记录起病、症状演变及外院诊疗经过',
            }
        ],
    }

    profile = infer_document_source_phase(metadata, ['现病史'])

    assert profile.phases == ('admission',)


def test_latest_available_outcome_makes_baseline_context_and_discharge_primary():
    baseline = infer_document_source_phase(
        _document_metadata('history', '入院概况', '入院时症状'),
        ['history'],
    )
    discharge = infer_document_source_phase(
        _document_metadata('status', '出院转归', '出院时治疗结果'),
        ['status'],
    )

    target = resolve_outcome_target_phase('', [baseline, discharge])

    assert target == 'discharge'
    assert classify_outcome_source_role(
        baseline,
        target_phase=target,
        source_kind='document',
    ) == 'CONTEXT'
    assert classify_outcome_source_role(
        discharge,
        target_phase=target,
        source_kind='document',
    ) == 'PRIMARY'


def test_explicit_admission_outcome_can_use_admission_phase_as_primary():
    baseline = infer_document_source_phase(
        _document_metadata('history', '入院概况', '入院时症状'),
        ['history'],
    )

    target = resolve_outcome_target_phase('admission', [baseline])

    assert target == 'admission'
    assert classify_outcome_source_role(
        baseline,
        target_phase=target,
        source_kind='document',
    ) == 'PRIMARY'


def test_future_service_role_depends_on_capability_not_skill_id():
    entity_only_service = {
        'service_id': 'future-clinical-service',
        'semantic': {'evidence_types': ['symptom_evidence']},
    }
    outcome_service = {
        'service_id': 'another-future-service',
        'semantic': {'evidence_capabilities': {'outcome_state': True}},
    }

    assert source_supports_outcome_state(entity_only_service) is False
    assert classify_outcome_source_role(
        {},
        target_phase='discharge',
        source_kind='service',
        supports_outcome_state=source_supports_outcome_state(entity_only_service),
    ) == 'SUPPORTING'
    assert source_supports_outcome_state(outcome_service) is True
    assert classify_outcome_source_role(
        {},
        target_phase='discharge',
        source_kind='service',
        supports_outcome_state=source_supports_outcome_state(outcome_service),
    ) == 'PRIMARY'


def test_baseline_non_improvement_does_not_conflict_with_discharge_improvement():
    result = build_condition_result(
        {
            'condition': '背痛好转',
            'files': [
                _file_result('baseline', 'NOT_MATCHED', 'CONTEXT'),
                _file_result('discharge', 'MATCHED', 'PRIMARY'),
            ],
        },
        'c-outcome',
    )

    assert result.status == EvidenceStatus.MATCHED
    assert result.conflict_level == ConflictLevel.NONE


def test_same_outcome_phase_still_reports_conclusive_conflict():
    result = build_condition_result(
        {
            'condition': '背痛好转',
            'files': [
                _file_result('discharge-a', 'MATCHED', 'PRIMARY'),
                _file_result('discharge-b', 'NOT_MATCHED', 'PRIMARY'),
            ],
        },
        'c-outcome-conflict',
    )

    assert result.status == EvidenceStatus.UNKNOWN
    assert result.conflict_level == ConflictLevel.CONCLUSIVE_CONFLICT


def test_outcome_route_enrichment_builds_structured_ir():
    analysis = {
        'conditions': [
            {
                'text': '背痛好转',
                'entity': '背痛好转',
                'canonical_entity': '背痛好转',
            }
        ]
    }

    enriched = augment_analysis_routes(
        analysis,
        '住院时间大于1天并且背痛好转的患者',
    )
    condition = enriched['conditions'][0]

    assert condition['entity'] == '背痛'
    assert condition['canonical_entity'] == '背痛'
    assert condition['predicate'] == 'outcome'
    assert condition['attributes']['outcome_state'] == 'improved'
    assert condition['attributes']['outcome_phase_policy'] == 'latest_available_outcome'
