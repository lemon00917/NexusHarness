from datetime import datetime

from microharness.medical.lab_rules import judge_lab_condition
from microharness.medical.time_window import TimeWindow


def _neutrophil_bindings():
    return [
        {"html_field": "[检验34] 化验项目描述", "eng_field": "inspItemDesc", "value": "中性粒细胞数"},
        {"html_field": "[检验34] 缩写", "eng_field": "inspItemAbbr", "value": "NEUT#"},
        {"html_field": "[检验34] 结果", "eng_field": "inspectionValue", "value": "4.00"},
        {"html_field": "[检验34] 单位", "eng_field": "inspResultUnitCode", "value": "*10^9/L"},
        {"html_field": "[检验34] 异常标志", "eng_field": "inspAbnoFlag", "value": "无"},
        {"html_field": "[检验34] 参考范围", "eng_field": "inspResultRange", "value": "2-7.7"},
        {"html_field": "[检验34] 检测日期", "eng_field": "inspectionDate", "value": "2026-06-09"},
        {"html_field": "[检验34] 检测时间", "eng_field": "inspectionTime", "value": "13:15:33"},
    ]


def _pre_surgery_48h_window():
    return TimeWindow(
        scope="事件前时间窗",
        start=datetime(2026, 6, 8, 15, 29),
        end=datetime(2026, 6, 10, 15, 29),
        required=True,
    )


def test_lab_low_condition_does_not_treat_time_window_as_threshold():
    result = judge_lab_condition(
        "术前48小时内中性粒细胞数偏低",
        _neutrophil_bindings(),
        _pre_surgery_48h_window(),
    )

    assert result["applicable"] is True
    assert result["matched"] is False
    assert result["keyword"] == "中性粒细胞数"
    assert result["candidate_count"] == 1
    assert "不小于等于 48" not in result["reason"]
    assert "异常状态：参考范围内" in result["reason"]
    assert result["candidate_records"][0]["数值判断"] == "异常状态：参考范围内"


def test_lab_explicit_numeric_condition_keeps_value_threshold_after_time_scope():
    result = judge_lab_condition(
        "术前48小时内中性粒细胞数>1.5x10^9/L",
        _neutrophil_bindings(),
        _pre_surgery_48h_window(),
    )

    assert result["applicable"] is True
    assert result["matched"] is True
    assert result["keyword"] == "中性粒细胞数"
    assert result["candidate_count"] == 1
    assert "结果满足：4×10^9 > 1.5×10^9" in result["reason"]
