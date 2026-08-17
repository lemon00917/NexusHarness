from microharness.medical.encounter_consistency import (
    EncounterConsistencyStatus,
    assess_encounter_consistency,
    build_source_encounter_profiles,
    requires_encounter_consistency,
)
from microharness.medical.evidence import (
    ConflictLevel,
    EvidenceStatus,
    ReasonCode,
    build_condition_result,
)


def _result(status="MATCHED"):
    return {
        "file": "encounter duration source",
        "status": status,
        "matched": status == "MATCHED",
        "reason": "duration met",
        "required_capabilities": ["ENCOUNTER_PERIOD", "NUMERIC_VALUE"],
    }


def _source(name, start, end, *, patient_id="", encounter_id="", record="r1"):
    bindings = []
    if patient_id:
        bindings.append({"html_field": f"[{record}] patientKey", "value": patient_id})
    if encounter_id:
        bindings.append({"html_field": f"[{record}] visitKey", "value": encounter_id})
    bindings.extend([
        {"html_field": f"[{record}] admissionDateTime", "value": start},
        {"html_field": f"[{record}] dischargeDateTime", "value": end},
    ])
    return {"file": name, "bindings": bindings}


def test_requires_encounter_consistency_only_when_period_capability_declared():
    assert requires_encounter_consistency([_result()]) is True
    assert requires_encounter_consistency([{"required_capabilities": ["NUMERIC_VALUE"]}]) is False
    assert requires_encounter_consistency([]) is False


def test_same_encounter_and_same_period_is_consistent():
    assessment = assess_encounter_consistency([
        _result(),
    ], [
        _source("document", "2026-03-03 09:37:22", "2026-03-05 12:00:00", encounter_id="v1"),
        _source("service", "2026-03-03 09:37:22", "2026-03-05 12:00:00", encounter_id="v1"),
    ])

    assert assessment.status == EncounterConsistencyStatus.CONSISTENT
    assert assessment.blocks_adjudication is False
    assert len(assessment.profiles) == 2


def test_same_encounter_with_small_time_variance_does_not_block():
    assessment = assess_encounter_consistency([_result()], [
        _source("document", "2026-03-03 09:37:22", "2026-03-05 12:00:00", encounter_id="v1"),
        _source("service", "2026-03-03 10:00:00", "2026-03-05 12:30:00", encounter_id="v1"),
    ])

    assert assessment.status == EncounterConsistencyStatus.MINOR_VARIANCE
    assert assessment.blocks_adjudication is False


def test_different_encounter_ids_block_adjudication():
    assessment = assess_encounter_consistency([_result()], [
        _source("document", "2026-03-03 09:37:22", "2026-03-05 12:00:00", encounter_id="v1"),
        _source("service", "2026-03-03 09:37:22", "2026-03-05 12:00:00", encounter_id="v2"),
    ])

    assert assessment.status == EncounterConsistencyStatus.ENCOUNTER_CONFLICT
    assert assessment.blocks_adjudication is True
    assert assessment.conflicts[0]["kind"] == "ENCOUNTER_ID_MISMATCH"


def test_clearly_separated_periods_without_ids_block_adjudication():
    assessment = assess_encounter_consistency([_result()], [
        _source("document", "2024-09-12 09:12:00", "2024-09-15 08:36:00"),
        _source("service", "2026-03-03 09:37:22", "2026-03-05 12:00:00"),
    ])

    assert assessment.status == EncounterConsistencyStatus.ENCOUNTER_CONFLICT
    assert assessment.blocks_adjudication is True
    assert assessment.conflicts[0]["kind"] == "PERIOD_SEPARATION"


def test_separated_periods_with_independent_matching_results_do_not_block():
    assessment = assess_encounter_consistency([
        {
            **_result(),
            "file": "document",
            "status": "匹配",
        },
        {
            **_result(),
            "file": "service",
            "status": "MATCHED",
        },
    ], [
        _source("document", "2024-09-12 09:12:00", "2024-09-15 08:36:00"),
        _source("service", "2026-03-03 09:37:22", "2026-03-05 12:00:00"),
    ])

    assert assessment.status == EncounterConsistencyStatus.INDEPENDENT_AGREEMENT
    assert assessment.blocks_adjudication is False
    assert assessment.reason_code == "ENCOUNTER_CONTEXT_INDEPENDENT_AGREEMENT"
    assert assessment.conflicts[0]["kind"] == "PERIOD_SEPARATION"


def test_separated_periods_with_different_results_still_block():
    assessment = assess_encounter_consistency([
        {**_result(), "file": "document", "status": "MATCHED"},
        {**_result("NOT_MATCHED"), "file": "service"},
    ], [
        _source("document", "2024-09-12 09:12:00", "2024-09-15 08:36:00"),
        _source("service", "2026-03-03 09:37:22", "2026-03-05 12:00:00"),
    ])

    assert assessment.status == EncounterConsistencyStatus.ENCOUNTER_CONFLICT
    assert assessment.blocks_adjudication is True


def test_multiple_complete_periods_in_one_source_without_identity_is_ambiguous():
    source = {"file": "unknown service", "bindings": []}
    source["bindings"].extend(_source("unused", "2026-01-01", "2026-01-03", record="r1")["bindings"])
    source["bindings"].extend(_source("unused", "2026-02-01", "2026-02-03", record="r2")["bindings"])

    assessment = assess_encounter_consistency([_result()], [source])

    assert assessment.status == EncounterConsistencyStatus.ENCOUNTER_CONFLICT
    assert assessment.blocks_adjudication is True
    assert assessment.conflicts[0]["kind"] == "AMBIGUOUS_SOURCE_ENCOUNTERS"


def test_consistency_uses_only_selected_candidate_records_when_available():
    source = {
        "file": "encounter-info",
        "service_id": "encounter-info",
        "bindings": [
            {"html_field": "[visit1] admissionDateTime", "value": "2026-01-01 09:00:00", "record_id": "v1"},
            {"html_field": "[visit1] dischargeDateTime", "value": "2026-01-03 09:00:00", "record_id": "v1"},
            {"html_field": "[visit2] admissionDateTime", "value": "2026-03-03 09:37:22", "record_id": "v2"},
            {"html_field": "[visit2] dischargeDateTime", "value": "2026-03-05 12:00:00", "record_id": "v2"},
        ],
    }
    result = {
        **_result(),
        "file": "encounter-info",
        "candidate_records": [{"record_id": "v2", "status": "MATCHED"}],
    }

    assessment = assess_encounter_consistency([result], [source])

    assert assessment.blocks_adjudication is False
    assert len(assessment.profiles) == 1
    assert assessment.profiles[0].record_key == "v2"


def test_consistency_ignores_non_decisive_missing_capability_source():
    assessment = assess_encounter_consistency([
        {
            **_result("UNKNOWN"),
            "file": "document",
            "source_role_decisive": False,
            "missing_capabilities": ["ENCOUNTER_PERIOD"],
        },
        {
            **_result(),
            "file": "service",
            "source_role_decisive": True,
        },
    ], [
        _source("document", "2024-09-12 09:12:00", "2024-09-15 08:36:00"),
        _source("service", "2026-03-03 09:37:22", "2026-03-05 12:00:00", encounter_id="174"),
    ])

    assert assessment.status == EncounterConsistencyStatus.INSUFFICIENT_IDENTITY
    assert assessment.blocks_adjudication is False
    assert len(assessment.profiles) == 1
    assert assessment.profiles[0].source_name == "service"


def test_future_skill_can_participate_with_declared_field_roles():
    source = {
        "file": "future skill",
        "service_id": "future-skill",
        "semantic": {
            "field_roles": {
                "patient_id": ["subjectKey"],
                "encounter_id": ["caseKey"],
                "encounter_start": ["startedAt"],
                "encounter_end": ["endedAt"],
            }
        },
        "bindings": [
            {"html_field": "subjectKey", "value": "p1"},
            {"html_field": "caseKey", "value": "e1"},
            {"html_field": "startedAt", "value": "2026-03-03 09:37:22"},
            {"html_field": "endedAt", "value": "2026-03-05 12:00:00"},
        ],
    }

    profiles = build_source_encounter_profiles(source)

    assert len(profiles) == 1
    assert profiles[0].patient_id == "p1"
    assert profiles[0].encounter_id == "e1"
    assert profiles[0].has_complete_period is True


def test_only_one_complete_source_does_not_block():
    assessment = assess_encounter_consistency([_result()], [
        _source("service", "2026-03-03 09:37:22", "2026-03-05 12:00:00", encounter_id="v1"),
        {"file": "document", "bindings": [{"html_field": "chiefComplaint", "value": "pain"}]},
    ])

    assert assessment.status == EncounterConsistencyStatus.INSUFFICIENT_IDENTITY
    assert assessment.blocks_adjudication is False


def test_blocking_assessment_forces_condition_unknown_but_preserves_evidence():
    unified = build_condition_result({
        "condition": "encounter duration > 1 day",
        "matched": True,
        "status": "MATCHED",
        "reason": "duration met",
        "files": [_result()],
        "encounter_consistency": {
            "status": "ENCOUNTER_CONFLICT",
            "blocks_adjudication": True,
            "reason": "period conflict",
        },
    }, "c1")

    assert unified.status == EvidenceStatus.UNKNOWN
    assert unified.reason_code == ReasonCode.ENCOUNTER_CONTEXT_CONFLICT
    assert unified.conflict_level == ConflictLevel.CONCLUSIVE_CONFLICT
    assert len(unified.evidence) == 1


def test_independent_agreement_preserves_conclusive_condition_result():
    unified = build_condition_result({
        "condition": "encounter duration > 1 day",
        "matched": True,
        "status": "MATCHED",
        "reason": "both sources independently calculated two days",
        "files": [
            {**_result(), "file": "document"},
            {**_result(), "file": "service"},
        ],
        "encounter_consistency": {
            "status": "INDEPENDENT_AGREEMENT",
            "blocks_adjudication": False,
            "reason": "separated periods, independent conclusions agree",
        },
    }, "c1")

    assert unified.status == EvidenceStatus.MATCHED
    assert unified.conflict_level == ConflictLevel.NONE
