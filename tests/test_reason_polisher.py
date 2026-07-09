from microharness.medical.reason_polisher import (
    _condition_fallback_explanation,
    _explanation_preserves_critical_facts,
    _useful_explanation,
)
from microharness.medical.structured_time import filter_bindings_by_time_window
from microharness.medical.time_window import TimeWindow
from datetime import datetime


def test_polished_explanation_cannot_invert_greater_than_condition():
    condition = "术前48小时内白细胞>1.5x10⁹/L"
    basis = "结果不满足：1.2×10^9 不大于 1.5×10^9"
    explanation = "患者术前48小时内白细胞计数低于1.5×10^9/L。"

    assert not _explanation_preserves_critical_facts(explanation, condition, basis)


def test_polished_explanation_allows_evidence_supported_comparison_wording():
    condition = "术前48小时内白细胞>1.5x10⁹/L"
    basis = "结果不满足：1.2×10^9 不大于 1.5×10^9"
    explanation = "患者术前48小时内白细胞结果不大于1.5×10^9/L，不符合条件。"

    assert _explanation_preserves_critical_facts(explanation, condition, basis)


def test_polished_explanation_cannot_turn_outside_window_candidate_into_absence():
    condition = "术前48小时使用过泮托拉唑钠肠溶片"
    basis = "找到1条候选记录，但记录时间不在事件前时间窗（范围：2026-06-04至2026-06-06）"
    explanation = "患者没有使用过泮托拉唑钠肠溶片。"

    assert not _explanation_preserves_critical_facts(explanation, condition, basis)


def test_polished_explanation_must_preserve_outside_window_candidate_detail():
    basis = "共找到1条候选记录，但检测时间不在住院期间（范围：2026-03-03 至 2026-03-05）"

    assert not _useful_explanation("住院期间没有找到血红蛋白异常记录。", basis)
    assert _useful_explanation("找到血红蛋白候选记录，但检测时间不在住院期间范围内。", basis)


def test_condition_fallback_explanation_describes_drug_outside_time_window():
    info = {
        "判断状态": "不符合",
        "时间范围": {
            "scope": "事件前时间窗",
            "start": "2026-06-04 15:20:00",
            "end": "2026-06-06 15:20:00",
        },
        "files": [
            {
                "file": "用药医嘱查询 (7条)",
                "证据角色": "主证据",
                "reason": "找到1条候选记录，但记录时间不在事件前时间窗",
                "fields": "[用药2] 剂型: 片剂 | 单次剂量: 1.0 | 剂量单位: 片 | 频次: Qd | 用药途径: 口服 | 药物名称: 泮托拉唑钠肠溶片(40mg*28片) | 开立日期时间: 2026-06-04 10:32:35",
                "候选记录": [
                    {"记录": "[用药2]", "记录时间": "2026-06-04 10:32:35", "是否在时间窗": False}
                ],
            }
        ],
    }

    text = _condition_fallback_explanation(info)

    assert "泮托拉唑钠肠溶片" in text
    assert "2026-06-04 10:32:35" in text
    assert "不在目标时间范围内" in text
    assert "口服" in text


def test_condition_fallback_explanation_uses_generic_candidate_fields_for_diagnosis():
    info = {
        "判断状态": "不符合",
        "files": [
            {
                "file": "诊断查询 (3条)",
                "证据角色": "主证据",
                "reason": "找到1条候选记录，但记录时间不在目标时间范围内",
                "候选记录": [
                    {
                        "记录": "[诊断1]",
                        "诊断名称": "2型糖尿病",
                        "诊断类型": "出院诊断",
                        "诊断时间": "2026-06-08 09:10:00",
                        "是否在时间窗": False,
                    }
                ],
            }
        ],
    }

    text = _condition_fallback_explanation(info)

    assert "2型糖尿病" in text
    assert "出院诊断" in text
    assert "2026-06-08 09:10:00" in text
    assert "不在目标时间范围内" in text


def test_condition_fallback_explanation_uses_generic_candidate_fields_for_document_section():
    info = {
        "判断状态": "符合",
        "files": [
            {
                "file": "入院记录",
                "证据角色": "主证据",
                "reason": "章节命中相关描述",
                "候选记录": [
                    {
                        "记录": "证据1",
                        "章节": "现病史",
                        "记录时间": "2026-06-01 08:00:00",
                        "描述": "胸背部疼痛3月，加重1月",
                        "是否在时间窗": True,
                    }
                ],
            }
        ],
    }

    text = _condition_fallback_explanation(info)

    assert "现病史" in text
    assert "胸背部疼痛" in text
    assert "2026-06-01 08:00:00" in text
    assert "在时间范围内" in text


def test_condition_fallback_explanation_describes_lab_numeric_failure():
    info = {
        "判断状态": "不符合",
        "files": [
            {
                "file": "检验指标查询 (1条)",
                "证据角色": "主证据",
                "reason": "共找到1条检验项目记录，但结果均不符合",
                "候选记录": [
                    {
                        "记录": "[检验29]",
                        "项目": "白细胞",
                        "检测时间": "2026-03-13 13:15:33",
                        "结果": "4.00",
                        "单位": "×10^9/L",
                        "数值判断": "结果不满足：4×10^9 不大于 1.5×10^9",
                        "数值是否满足": False,
                        "是否在时间窗": False,
                    }
                ],
            }
        ],
    }

    text = _condition_fallback_explanation(info)

    assert "[检验29]" in text
    assert "白细胞" in text
    assert "2026-03-13 13:15:33" in text
    assert "结果为4.00×10^9/L" in text
    assert "结果不满足" in text


def test_structured_time_candidate_records_keep_source_fields():
    bindings = [
        {"html_field": "[用药1] 药物名称", "value": "阿司匹林肠溶片"},
        {"html_field": "[用药1] 开立日期时间", "value": "2026-04-28 14:21:10"},
        {"html_field": "[用药1] 用药途径", "value": "口服"},
    ]
    window = TimeWindow(
        scope="事件前时间窗",
        start=datetime(2026, 6, 9, 15, 29),
        end=datetime(2026, 6, 10, 15, 29),
        required=True,
    )

    result = filter_bindings_by_time_window(bindings, window)

    assert result["applicable"] is True
    assert result["matched"] is False
    assert "阿司匹林肠溶片" in result["reason"]
    assert "开立日期时间=2026-04-28 14:21:10" in result["reason"]
    assert result["candidate_records"][0]["药物名称"] == "阿司匹林肠溶片"
    assert result["candidate_records"][0]["用药途径"] == "口服"
