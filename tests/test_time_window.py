from datetime import datetime

from microharness.medical.query_ir import TemporalIR
from microharness.medical.time_window import (
    get_anchor_route_for_condition,
    resolve_time_window,
)


def _encounter_results(*bindings):
    return {
        "encounter-info": [
            {
                "file": "就诊信息查询 (1条)",
                "bindings": list(bindings),
            }
        ]
    }


def _binding(field, value, label=""):
    return {
        "eng_field": field,
        "html_field": label,
        "html_value": value,
    }


def test_discharge_after_window_only_requires_discharge_time():
    services = _encounter_results(
        _binding("encEndDate", "2026-06-08"),
        _binding("encEndTime", "11:00:00"),
    )

    window = resolve_time_window("出院后10天内血红蛋白指标异常", services)

    assert window.resolved
    assert window.start == datetime(2026, 6, 8, 11, 0, 0)
    assert window.end == datetime(2026, 6, 18, 11, 0, 0)
    assert window.source == "encounter-info"


def test_discharge_before_window_uses_discharge_time():
    services = _encounter_results(
        _binding("encStartDate", "2026-06-01"),
        _binding("encEndDate", "2026-06-08"),
        _binding("encEndTime", "11:00:00"),
    )

    window = resolve_time_window("出院前2天内白细胞偏低", services)

    assert window.start == datetime(2026, 6, 6, 11, 0, 0)
    assert window.end == datetime(2026, 6, 8, 11, 0, 0)


def test_admission_after_window_only_requires_admission_time():
    services = _encounter_results(
        _binding("encStartDate", "2026-06-01"),
        _binding("encStartTime", "09:30:00"),
    )

    window = resolve_time_window("入院后3天内血红蛋白异常", services)

    assert window.start == datetime(2026, 6, 1, 9, 30, 0)
    assert window.end == datetime(2026, 6, 4, 9, 30, 0)


def test_missing_discharge_time_reports_discharge_anchor_error():
    services = _encounter_results(
        _binding("encStartDate", "2026-06-01"),
        _binding("encStartTime", "09:30:00"),
    )

    window = resolve_time_window("出院后10天内血红蛋白指标异常", services)

    assert not window.resolved
    assert window.reason == "缺少出院时间，可能仍在住院"


def test_inpatient_window_still_requires_admission_time():
    services = _encounter_results(
        _binding("encEndDate", "2026-06-08"),
        _binding("encEndTime", "11:00:00"),
    )

    window = resolve_time_window("住院期间血红蛋白指标异常", services)

    assert not window.resolved
    assert window.reason == "缺少入院时间"


def test_structured_discharge_window_does_not_reparse_condition_text():
    services = _encounter_results(
        _binding("encEndDate", "2026-06-08"),
        _binding("encEndTime", "11:00:00"),
    )
    temporal = TemporalIR(
        scope="event_window",
        event="discharge",
        relation="after",
        duration=10,
        unit="天",
    )

    window = resolve_time_window(
        "血红蛋白指标异常",
        services,
        temporal=temporal,
        allow_text_fallback=False,
    )

    assert window.start == datetime(2026, 6, 8, 11, 0, 0)
    assert window.end == datetime(2026, 6, 18, 11, 0, 0)


def test_structured_admission_window_uses_admission_anchor():
    services = _encounter_results(
        _binding("encStartDate", "2026-06-01"),
        _binding("encStartTime", "09:30:00"),
    )
    temporal = TemporalIR(
        scope="event_window",
        event="admission",
        relation="before",
        duration=24,
        unit="小时",
    )

    window = resolve_time_window(
        "任意无关文本",
        services,
        temporal=temporal,
        allow_text_fallback=False,
    )

    assert window.start == datetime(2026, 5, 31, 9, 30, 0)
    assert window.end == datetime(2026, 6, 1, 9, 30, 0)


def test_structured_encounter_scope_uses_full_encounter_window():
    services = _encounter_results(
        _binding("encStartDate", "2026-06-01"),
        _binding("encStartTime", "09:30:00"),
        _binding("encEndDate", "2026-06-03"),
        _binding("encEndTime", "10:00:00"),
    )
    temporal = TemporalIR(
        scope="encounter",
        event="encounter",
        relation="during",
    )

    window = resolve_time_window(
        "血红蛋白指标异常",
        services,
        temporal=temporal,
        allow_text_fallback=False,
    )

    assert window.scope == "住院期间"
    assert window.start == datetime(2026, 6, 1, 9, 30, 0)
    assert window.end == datetime(2026, 6, 3, 10, 0, 0)


def test_structured_surgery_event_routes_by_anchor_metadata(monkeypatch):
    monkeypatch.setattr(
        "microharness.medical.time_window._anchor_specs",
        lambda: [
            {
                "doc": "手术记录",
                "label": "手术日期",
                "aliases": ["手术", "术"],
                "event": "",
                "source": "手术记录.手术日期",
                "time_role": "range",
            }
        ],
    )
    temporal = TemporalIR(
        scope="event_window",
        event="surgery",
        relation="before",
        duration=48,
        unit="小时",
    )

    docs, sections = get_anchor_route_for_condition(
        "中性粒细胞数大于阈值",
        temporal=temporal,
        allow_text_fallback=False,
    )

    assert docs == ["手术记录"]
    assert sections == ["手术日期"]


def test_structured_surgery_window_uses_document_anchor(monkeypatch):
    monkeypatch.setattr(
        "microharness.medical.time_window._anchor_specs",
        lambda: [
            {
                "doc": "手术记录",
                "label": "手术日期",
                "aliases": ["手术", "术"],
                "event": "surgery",
                "source": "手术记录.手术日期",
                "time_role": "range",
            }
        ],
    )
    records = [
        {
            "file": "手术记录 (1条)",
            "bindings": [
                _binding("operationDate", "2026-06-10 15:20:00", "手术日期")
            ],
        }
    ]
    temporal = TemporalIR(
        scope="event_window",
        event="surgery",
        relation="before",
        duration=48,
        unit="小时",
        selection="last",
    )

    window = resolve_time_window(
        "中性粒细胞数大于阈值",
        {},
        records,
        temporal=temporal,
        allow_text_fallback=False,
    )

    assert window.start == datetime(2026, 6, 8, 15, 20, 0)
    assert window.end == datetime(2026, 6, 10, 15, 20, 0)
    assert window.source == "手术记录.手术日期"


def test_disabling_text_fallback_prevents_raw_temporal_reparse():
    services = _encounter_results(
        _binding("encEndDate", "2026-06-08"),
        _binding("encEndTime", "11:00:00"),
    )

    window = resolve_time_window(
        "出院后10天内血红蛋白指标异常",
        services,
        allow_text_fallback=False,
    )

    assert window is None
