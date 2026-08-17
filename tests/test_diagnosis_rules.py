from datetime import datetime

from microharness.medical.diagnosis_rules import judge_diagnosis_condition
from microharness.medical.time_window import TimeWindow


def _bindings(*records):
    result = []
    field_map = {
        "name": ("诊断名称", "diagnoseName"),
        "type": ("诊断类型", "diagTypeDesc"),
        "status": ("诊断状态", "diagStatusDesc"),
        "remarks": ("诊断备注", "diagnoseRemarks"),
        "date": ("诊断日期", "diagnoseDate"),
        "time": ("诊断时间", "diagnoseTime"),
    }
    for index, record in enumerate(records, 1):
        for key, value in record.items():
            label, eng = field_map[key]
            result.append(
                {
                    "html_field": f"[诊断{index}] {label}",
                    "value": value,
                    "xml_path": f"external/{eng}",
                    "eng_field": eng,
                }
            )
    return result


def _judge(bindings, **kwargs):
    return judge_diagnosis_condition(
        kwargs.pop("condition", "患有胃息肉"),
        bindings,
        entity=kwargs.pop("entity", "胃息肉"),
        semantic={"domain": "diagnosis", "entity_type": "diagnosis"},
        **kwargs,
    )


def test_confirmed_diagnosis_is_matched():
    result = _judge(_bindings({"name": "胃息肉", "type": "出院诊断", "status": "有效"}))

    assert result["applicable"] is True
    assert result["status"] == "MATCHED"
    assert result["reason_code"] == "DIAGNOSIS_CONFIRMED"


def test_uncertain_diagnosis_status_is_unknown():
    result = _judge(_bindings({"name": "胃息肉", "type": "入院诊断", "status": "疑似"}))

    assert result["status"] == "UNKNOWN"
    assert result["reason_code"] == "DIAGNOSIS_UNCERTAIN"


def test_uncertainty_in_diagnosis_name_is_unknown():
    result = _judge(_bindings({"name": "胃息肉待排", "type": "入院诊断"}))

    assert result["status"] == "UNKNOWN"


def test_excluded_diagnosis_is_not_matched():
    result = _judge(_bindings({"name": "胃息肉", "status": "已排除"}))

    assert result["status"] == "NOT_MATCHED"
    assert result["reason_code"] == "DIAGNOSIS_EXCLUDED"


def test_confirmed_record_wins_over_uncertain_candidate():
    result = _judge(
        _bindings(
            {"name": "胃息肉", "status": "疑似"},
            {"name": "胃息肉", "type": "出院诊断", "status": "有效"},
        )
    )

    assert result["status"] == "MATCHED"
    assert result["candidate_count"] == 2


def test_local_ordered_character_variant_is_supported():
    result = _judge(
        _bindings({"name": "胸背部疼痛", "type": "入院诊断"}),
        condition="背痛",
        entity="背痛",
    )

    assert result["status"] == "MATCHED"


def test_outpatient_diagnosis_candidate_includes_full_diagnosis_time():
    result = _judge(
        _bindings(
            {"name": "慢性胃炎", "type": "出院诊断"},
            {
                "name": "背痛",
                "type": "门诊诊断",
                "date": "2026-03-10",
                "time": "10:09:40",
            },
        ),
        condition="背痛",
        entity="背痛",
    )

    assert result["status"] == "MATCHED"
    assert result["candidate_count"] == 1
    assert result["candidate_records"][0]["记录"] == "诊断2"
    assert result["candidate_records"][0]["诊断名称"] == "背痛"
    assert result["candidate_records"][0]["诊断类型"] == "门诊诊断"
    assert result["candidate_records"][0]["诊断时间"] == "2026-03-10 10:09:40"
    assert "诊断状态=未取得" not in result["reason"]


def test_unrelated_diagnosis_is_not_mentioned():
    result = _judge(_bindings({"name": "慢性胃炎", "type": "出院诊断"}))

    assert result["applicable"] is True
    assert result["status"] == "NOT_MENTIONED"
    assert result["reason_code"] == "NO_MATCHING_RECORD"
    assert result["candidate_count"] == 0


def test_unresolved_window_can_use_configured_diagnosis_type_semantics():
    result = _judge(
        _bindings({"name": "胃息肉", "type": "出院诊断", "status": "有效"}),
        condition="出院诊断为胃息肉",
        time_window=TimeWindow(scope="出院时", required=True, reason="缺少出院时间"),
        temporal_semantics={
            "field": "diagTypeDesc",
            "rules": [
                {
                    "query_terms": ["出院"],
                    "values": ["出院诊断"],
                    "reason": "诊断类型为出院诊断",
                }
            ],
        },
    )

    assert result["status"] == "MATCHED"


def test_resolved_window_rejects_outside_diagnosis_record():
    result = _judge(
        _bindings(
            {
                "name": "胃息肉",
                "type": "补充诊断",
                "status": "有效",
                "date": "2026-06-01",
                "time": "08:00:00",
            }
        ),
        condition="住院期间胃息肉",
        time_window=TimeWindow(
            scope="住院期间",
            start=datetime(2026, 6, 2),
            end=datetime(2026, 6, 5),
            required=True,
        ),
    )

    assert result["status"] == "NOT_MATCHED"
    assert result["reason_code"] == "TIME_OUTSIDE_WINDOW"
