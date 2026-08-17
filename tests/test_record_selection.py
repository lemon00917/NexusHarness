from microharness.medical.evidence import EvidenceStatus
from microharness.medical.record_selection import adjudicate_record_quantifier


def _record(
    name: str,
    status: str,
    *,
    scope: str = "NOT_REQUIRED",
    event_time: str = "2026-06-01 10:00:00",
):
    return {
        "name": name,
        "record_status": status,
        "scope_status": scope,
        "event_time": event_time,
    }


def _judge(records, mode, count=None, **kwargs):
    return adjudicate_record_quantifier(
        records,
        {"mode": mode, "count": count, "unit": "次"},
        fallback_status=EvidenceStatus.NOT_MATCHED,
        candidate_count=kwargs.pop("candidate_count", len(records)),
        **kwargs,
    )


def test_any_uses_a_matching_record_without_requiring_all_records():
    decision = _judge([
        _record("r1", "NOT_MATCHED"),
        _record("r2", "MATCHED"),
    ], "any")

    assert decision.status == EvidenceStatus.MATCHED
    assert decision.reason_code == "QUANTIFIER_ANY_MATCHED"
    assert decision.selected_records[0]["name"] == "r2"


def test_all_fails_when_one_in_scope_record_fails():
    decision = _judge([
        _record("r1", "MATCHED"),
        _record("r2", "NOT_MATCHED"),
    ], "all")

    assert decision.status == EvidenceStatus.NOT_MATCHED
    assert decision.reason_code == "QUANTIFIER_ALL_NOT_MET"


def test_out_of_scope_record_is_not_counted_as_a_failed_in_scope_record():
    decision = _judge([
        _record("inside", "MATCHED", scope="IN_SCOPE"),
        _record("outside", "NOT_MATCHED", scope="OUT_OF_SCOPE"),
    ], "all")

    assert decision.status == EvidenceStatus.MATCHED
    assert decision.record_status_counts["out_of_scope"] == 1


def test_at_least_can_be_proved_even_when_an_extra_record_is_unknown():
    decision = _judge([
        _record("r1", "MATCHED"),
        _record("r2", "MATCHED"),
        _record("r3", "UNKNOWN"),
    ], "at_least", 2)

    assert decision.status == EvidenceStatus.MATCHED
    assert decision.reason_code == "QUANTIFIER_COUNT_MATCHED"


def test_exact_count_is_unknown_when_an_unknown_record_can_change_the_answer():
    decision = _judge([
        _record("r1", "MATCHED"),
        _record("r2", "UNKNOWN"),
    ], "exact", 1)

    assert decision.status == EvidenceStatus.UNKNOWN
    assert decision.reason_code == "QUANTIFIER_COUNT_INDETERMINATE"


def test_latest_selects_by_canonical_event_time_then_uses_record_status():
    decision = _judge([
        _record("older", "MATCHED", event_time="2026-06-01 10:00:00"),
        _record("newer", "NOT_MATCHED", event_time="2026-06-02 10:00:00"),
    ], "latest")

    assert decision.status == EvidenceStatus.NOT_MATCHED
    assert decision.selected_records[0]["name"] == "newer"


def test_latest_is_unknown_when_any_in_scope_candidate_lacks_time():
    decision = _judge([
        _record("timed", "MATCHED"),
        _record("untimed", "MATCHED", event_time=""),
    ], "latest")

    assert decision.status == EvidenceStatus.UNKNOWN
    assert decision.reason_code == "QUANTIFIER_RECORD_TIME_MISSING"


def test_all_is_unknown_when_candidate_materialization_is_incomplete():
    decision = _judge(
        [_record("r1", "MATCHED")],
        "all",
        candidate_count=2,
    )

    assert decision.status == EvidenceStatus.UNKNOWN
    assert decision.selection_complete is False


def test_missing_candidate_count_is_used_for_upper_bound_uncertainty():
    decision = _judge(
        [_record("r1", "MATCHED")],
        "at_most",
        2,
        candidate_count=5,
    )

    assert decision.status == EvidenceStatus.UNKNOWN
    assert decision.record_status_counts["unmaterialized"] == 4


def test_missing_candidate_count_prevents_false_impossibility_for_lower_bound():
    decision = _judge(
        [_record("r1", "MATCHED")],
        "at_least",
        3,
        candidate_count=5,
    )

    assert decision.status == EvidenceStatus.UNKNOWN
    assert decision.record_status_counts["unmaterialized"] == 4


def test_declared_candidates_without_materialized_records_are_unknown():
    decision = _judge([], "any", candidate_count=3)

    assert decision.status == EvidenceStatus.UNKNOWN
    assert decision.reason_code == "QUANTIFIER_SOURCE_UNKNOWN"
    assert decision.record_status_counts["unmaterialized"] == 3


def test_unsupported_sequence_quantifier_fails_closed():
    decision = _judge([_record("r1", "MATCHED")], "consecutive")

    assert decision.status == EvidenceStatus.UNKNOWN
    assert decision.reason_code == "QUANTIFIER_UNSUPPORTED"
