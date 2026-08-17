from datetime import datetime

from microharness.medical.evidence import build_condition_result
from microharness.medical.medication_rules import judge_medication_condition
from microharness.medical.time_window import TimeWindow


BASE_SEMANTIC = {
    "domain": "medication",
    "entity_type": "drug",
    "predicate": "administered",
    "evidence_capabilities": {"ordered": True, "administered": True, "status": True},
    "fields": {
        "entity": ["orderName"],
        "record_id": ["medPrescNo"],
        "ordered_at": ["开立日期时间"],
        "administered_at": ["executeDateTime"],
        "status": ["ordStatusDesc"],
    },
    "predicate_policies": {
        "administered": {
            "event_time_role": "ordered_at",
            "required_status": True,
            "accepted_status_values": ["核实", "执行"],
            "rejected_status_values": ["作废", "撤销"],
        }
    },
}


def _bindings(
    *,
    ordered_at="2026-06-10 10:00:00",
    administered_at="",
    name="氯吡格雷片",
    status="执行",
):
    rows = [
        {"html_field": "[用药1] 药物名称", "eng_field": "orderName", "value": name},
        {"html_field": "[用药1] 处方号", "eng_field": "medPrescNo", "value": "RX-1"},
    ]
    if ordered_at:
        rows.append({"html_field": "[用药1] 开立日期时间", "eng_field": "开立日期时间", "value": ordered_at})
    if status:
        rows.append({"html_field": "[用药1] 医嘱状态描述", "eng_field": "ordStatusDesc", "value": status})
    if administered_at:
        rows.extend([
            {"html_field": "[用药1] 执行时间", "eng_field": "executeDateTime", "value": administered_at},
            {"html_field": "[用药1] 执行状态", "eng_field": "executeStatus", "value": "已执行"},
        ])
    return rows


def _semantic_with_ordered_policy():
    semantic = {**BASE_SEMANTIC}
    semantic['predicate_policies'] = {
        **BASE_SEMANTIC['predicate_policies'],
        'ordered': {
            'event_time_role': 'ordered_at',
            'required_status': True,
            'accepted_status_values': ['核实', '执行'],
            'rejected_status_values': ['作废', '撤销'],
        },
    }
    return semantic


def _window():
    return TimeWindow(
        scope="事件前时间窗",
        start=datetime(2026, 6, 9, 15, 29),
        end=datetime(2026, 6, 10, 15, 29),
        required=True,
    )


def test_arbitrary_medication_name_uses_generic_entity_matching():
    result = judge_medication_condition(
        "术前24小时开立过氯吡格雷",
        _bindings(),
        entity="氯吡格雷",
        time_window=_window(),
        semantic=BASE_SEMANTIC,
    )

    assert result["status"] == "MATCHED"
    assert result["candidate_records"][0]["医嘱项"] == "氯吡格雷片"
    assert result["candidate_records"][0]["开立时间"] == "2026-06-10 10:00:00"


def test_ordered_record_outside_window_is_not_matched():
    result = judge_medication_condition(
        "术前24小时开立过氯吡格雷",
        _bindings(ordered_at="2026-04-28 14:21:10"),
        entity="氯吡格雷",
        time_window=_window(),
        semantic=BASE_SEMANTIC,
    )

    assert result["status"] == "NOT_MATCHED"
    assert result["reason_code"] == "TIME_OUTSIDE_WINDOW"
    assert result["candidate_records"][0]["是否在时间窗"] is False


def test_ordered_predicate_rejects_cancelled_order():
    result = judge_medication_condition(
        '术前开过氯吡格雷', _bindings(status='撤销'), entity='氯吡格雷',
        time_window=_window(), semantic=_semantic_with_ordered_policy(),
    )
    assert result['status'] == 'NOT_MATCHED'
    assert result['reason_code'] == 'STATUS_CONDITION_NOT_MET'
    assert result['candidate_records'][0]['状态是否满足'] is False
    assert '撤销' in result['reason']


def test_ordered_predicate_accepts_verified_order():
    bindings = _bindings(status='核实')
    bindings.append({'html_field': '[用药1] 用药途径', 'eng_field': 'medUsageDesc', 'value': '口服'})
    result = judge_medication_condition(
        '术前开过氯吡格雷', bindings, entity='氯吡格雷',
        time_window=_window(), semantic=_semantic_with_ordered_policy(),
    )
    assert result['status'] == 'MATCHED'
    assert result['candidate_records'][0]['状态是否满足'] is True
    assert '途径=口服' in result['reason']


def test_missing_anchor_is_unknown():
    unresolved = TimeWindow(scope="事件前时间窗", required=True, reason="缺少手术时间")
    result = judge_medication_condition(
        "术前24小时开立过氯吡格雷",
        _bindings(),
        entity="氯吡格雷",
        time_window=unresolved,
        semantic=BASE_SEMANTIC,
    )

    assert result["status"] == "UNKNOWN"
    assert result["reason_code"] == "MISSING_EVENT_TIME"


def test_missing_order_time_is_unknown_for_ordered_window():
    result = judge_medication_condition(
        "术前24小时开立过氯吡格雷",
        _bindings(ordered_at=""),
        entity="氯吡格雷",
        time_window=_window(),
        semantic=BASE_SEMANTIC,
    )

    assert result["status"] == "UNKNOWN"
    assert result["candidate_records"][0]["记录时间"] == "未取得"


def test_used_predicate_uses_order_time_and_valid_status():
    result = judge_medication_condition(
        "术前24小时使用过氯吡格雷",
        _bindings(),
        entity="氯吡格雷",
        time_window=_window(),
        semantic=BASE_SEMANTIC,
    )

    assert result["status"] == "MATCHED"
    assert result["reason_code"] == "MATCH_CONFIRMED"
    evidence = result["candidate_records"][0]
    assert evidence["是否在时间窗"] is True
    assert evidence["状态是否满足"] is True
    assert evidence["证据时间角色"] == "ordered_at"
    assert evidence["业务判定口径"] == "开立时间和医嘱状态"


def test_administered_time_in_window_is_matched_when_source_supports_it():
    semantic = {
        **BASE_SEMANTIC,
        "evidence_capabilities": {"ordered": True, "administered": True},
        "predicate_policies": {},
    }
    result = judge_medication_condition(
        "术前24小时使用过氯吡格雷",
        _bindings(administered_at="2026-06-10 11:30:00"),
        entity="氯吡格雷",
        time_window=_window(),
        semantic=semantic,
    )

    assert result["status"] == "MATCHED"
    evidence = result["candidate_records"][0]
    assert evidence["执行/给药时间"] == "2026-06-10 11:30:00"
    assert evidence["证据时间角色"] == "administered_at"


def test_used_predicate_outside_window_is_not_matched_with_valid_status():
    result = judge_medication_condition(
        "术前24小时使用过阿托伐他汀",
        _bindings(ordered_at="2026-04-28 14:21:10", name="阿托伐他汀钙片"),
        entity="阿托伐他汀",
        time_window=_window(),
        semantic=BASE_SEMANTIC,
    )

    assert result["status"] == "NOT_MATCHED"
    assert result["reason_code"] == "TIME_OUTSIDE_WINDOW"
    evidence = result["candidate_records"][0]
    assert evidence["是否在时间窗"] is False
    assert evidence["状态是否满足"] is True


def test_used_predicate_rejects_invalid_status_and_explains_both_checks():
    result = judge_medication_condition(
        "术前24小时使用过阿托伐他汀",
        _bindings(
            ordered_at="2026-04-28 14:21:10",
            name="阿托伐他汀钙片",
            status="撤销",
        ),
        entity="阿托伐他汀",
        time_window=_window(),
        semantic=BASE_SEMANTIC,
    )

    assert result["status"] == "NOT_MATCHED"
    assert result["reason_code"] == "STATUS_CONDITION_NOT_MET"
    evidence = result["candidate_records"][0]
    assert evidence["是否在时间窗"] is False
    assert evidence["状态是否满足"] is False
    assert "不在事件前时间窗" in result["reason"]
    assert "撤销" in result["reason"]


def test_used_predicate_with_missing_status_is_unknown():
    result = judge_medication_condition(
        "术前24小时使用过氯吡格雷",
        _bindings(status=""),
        entity="氯吡格雷",
        time_window=_window(),
        semantic=BASE_SEMANTIC,
    )

    assert result["status"] == "UNKNOWN"
    assert result["reason_code"] == "INSUFFICIENT_EVIDENCE"
    assert result["candidate_records"][0]["状态是否满足"] is None


def test_used_predicate_with_unrecognized_status_is_unknown():
    result = judge_medication_condition(
        "术前24小时使用过氯吡格雷",
        _bindings(status="待确认"),
        entity="氯吡格雷",
        time_window=_window(),
        semantic=BASE_SEMANTIC,
    )

    assert result["status"] == "UNKNOWN"
    assert "未配置有效性规则" in result["candidate_records"][0]["状态判断"]


def test_no_matching_medication_record_is_not_mentioned():
    result = judge_medication_condition(
        "开立过华法林",
        _bindings(),
        entity="华法林",
        semantic=BASE_SEMANTIC,
    )

    assert result["status"] == "NOT_MENTIONED"
    assert result["reason_code"] == "NO_MATCHING_RECORD"


def test_source_failure_is_unknown():
    result = judge_medication_condition(
        "使用过氯吡格雷",
        [{"html_field": "接口状态", "value": "未取得数据"}],
        entity="氯吡格雷",
        semantic=BASE_SEMANTIC,
    )

    assert result["status"] == "UNKNOWN"
    assert result["reason_code"] == "SOURCE_UNAVAILABLE"


def test_medication_candidate_is_adapted_to_canonical_evidence():
    judged = judge_medication_condition(
        "术前24小时开立过氯吡格雷",
        _bindings(),
        entity="氯吡格雷",
        time_window=_window(),
        semantic=BASE_SEMANTIC,
    )
    condition_result = build_condition_result(
        {
            "condition": "术前24小时开立过氯吡格雷",
            "matched": judged["matched"],
            "status": judged["status"],
            "reason": judged["reason"],
            "files": [{
                "file": "用药医嘱查询",
                "matched": judged["matched"],
                "status": judged["status"],
                "reason_code": judged["reason_code"],
                "reason": judged["reason"],
                "fields": judged["fields"],
                "候选记录": judged["candidate_records"],
            }],
        },
        "condition-1",
    ).to_dict()

    evidence = condition_result["evidence"][0]
    assert evidence["status"] == "MATCHED"
    assert evidence["entity"] == "氯吡格雷片"
    assert evidence["record_id"] == "RX-1"
    assert evidence["event_time"] == "2026-06-10 10:00:00"
    assert evidence["metadata"]["ordered_at"] == "2026-06-10 10:00:00"


def test_unknown_file_status_propagates_to_condition_status():
    judged = judge_medication_condition(
        "术前24小时使用过氯吡格雷",
        _bindings(status=""),
        entity="氯吡格雷",
        time_window=_window(),
        semantic=BASE_SEMANTIC,
    )
    condition_result = build_condition_result(
        {
            "condition": "术前24小时使用过氯吡格雷",
            "matched": False,
            "reason": judged["reason"],
            "files": [{
                "file": "用药医嘱查询",
                "matched": False,
                "status": judged["status"],
                "reason_code": judged["reason_code"],
                "reason": judged["reason"],
                "fields": judged["fields"],
                "候选记录": judged["candidate_records"],
            }],
        },
        "condition-1",
    ).to_dict()

    assert condition_result["status"] == "UNKNOWN"
    assert condition_result["conclusive"] is False
    assert condition_result["evidence"][0]["status"] == "UNKNOWN"
