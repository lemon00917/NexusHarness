from microharness.medical.reason_polisher import (
    _build_payload,
    _condition_basis,
    _condition_fallback_explanation,
    _condition_explanation_matches_status,
    _explanation_preserves_critical_facts,
    _explanation_status_claims_match,
    _overall_explanation_matches_status,
    _useful_explanation,
    polish_response_explanations,
)
from microharness.medical.structured_time import filter_bindings_by_time_window
from microharness.medical.time_window import TimeWindow
from datetime import datetime
import json
import pytest


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


def test_polished_explanation_cannot_reverse_subtraction_operands():
    condition = '住院天数小于5天'
    basis = '住院天数约等于出院日期时间 - 入院日期时间，计算结果为2天，小于5天'

    assert not _explanation_preserves_critical_facts(
        '住院天数约等于入院日期时间 - 出院日期时间，计算结果为2天，小于5天。',
        condition,
        basis,
    )
    assert _explanation_preserves_critical_facts(
        '住院天数约等于出院日期时间 - 入院日期时间，计算结果为2天，小于5天。',
        condition,
        basis,
    )


def test_polished_explanation_cannot_turn_outside_window_candidate_into_absence():
    condition = "术前48小时使用过泮托拉唑钠肠溶片"
    basis = "找到1条候选记录，但记录时间不在事件前时间窗（范围：2026-06-04至2026-06-06）"
    explanation = "患者没有使用过泮托拉唑钠肠溶片。"

    assert not _explanation_preserves_critical_facts(explanation, condition, basis)


def test_polished_explanation_must_preserve_outside_window_candidate_detail():
    basis = "共找到1条候选记录，但检测时间不在住院期间（范围：2026-03-03 至 2026-03-05）"

    assert not _useful_explanation("住院期间没有找到血红蛋白异常记录。", basis)
    assert _useful_explanation("找到血红蛋白候选记录，但检测时间不在住院期间范围内。", basis)


def test_explanation_rejects_unknown_date_or_numeric_fact():
    condition = "术前48小时内白细胞>1.5x10⁹/L"
    basis = "检测时间为2026-06-05 10:00:00，结果为4.0×10^9/L"

    assert _explanation_preserves_critical_facts(
        "检测时间为2026-06-05 10:00:00，结果为4.0×10^9/L，符合条件。",
        condition,
        basis,
    )
    assert not _explanation_preserves_critical_facts(
        "检测时间为2026-06-04 10:00:00，结果为5.0×10^9/L，符合条件。",
        condition,
        basis,
    )


def test_explanation_accepts_equivalent_date_and_numeric_formatting():
    condition = "术前48小时内白细胞>1.5x10⁹/L"
    basis = "检测时间为2026-06-05 10:00:00，结果为4.0×10^9/L"

    assert _explanation_preserves_critical_facts(
        "检测时间为2026/6/5 10:00:00，结果为4×10^9/L，符合条件。",
        condition,
        basis,
    )
    assert _explanation_preserves_critical_facts(
        "2026年6月5日检测结果为4×10^9/L，符合条件。",
        condition,
        basis,
    )


def test_explanation_status_claim_must_match_canonical_status():
    assert _explanation_status_claims_match("该条件判定为未提及。", "未提及")
    assert not _explanation_status_claims_match("该条件判定为不符合。", "未提及")


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


def test_condition_fallback_explanation_hides_record_identity_metadata():
    info = {
        "判断状态": "符合",
        "files": [
            {
                "file": "诊断查询 (1条)",
                "证据角色": "主证据",
                "候选记录": [
                    {
                        "记录": "诊断ID=00001_174||2",
                        "记录序号": "[诊断2]",
                        "记录ID": "00001_174||2",
                        "记录标识名称": "诊断ID",
                        "记录标识字段": "hosDiagId",
                        "record_id": "00001_174||2",
                        "record_id_label": "诊断ID",
                        "record_id_field": "hosDiagId",
                        "诊断名称": "背痛",
                        "诊断类型": "入院诊断",
                        "诊断时间": "2026-03-10 10:09:40",
                        "是否在时间窗": True,
                    }
                ],
            }
        ],
    }

    text = _condition_fallback_explanation(info)

    assert text.count("诊断ID=00001_174||2") == 1
    assert "诊断名称为背痛" in text
    assert "诊断类型为入院诊断" in text
    assert "记录标识名称" not in text
    assert "记录标识字段" not in text
    assert "记录ID为" not in text
    assert "record_id" not in text


def test_matched_condition_explanation_prefers_support_over_unavailable_source():
    info = {
        "matched": True,
        "判断状态": "符合",
        "files": [
            {
                "file": "出院记录",
                "证据角色": "主证据",
                "matched": True,
                "reason": "入院时间为2026-06-01，出院时间为2026-06-03，住院2天，小于5天",
            },
            {
                "file": "就诊信息查询 (未取得数据)",
                "证据角色": "主证据",
                "matched": False,
                "reason": "未取得就诊信息查询接口数据，当前无法用该结构化数据源判断",
            },
        ],
    }

    text = _condition_fallback_explanation(info)

    assert "住院2天" in text
    assert "未取得" not in text


def test_matched_condition_rejects_llm_explanation_that_only_reports_source_failure():
    info = {
        "matched": True,
        "判断状态": "符合",
        "files": [
            {"file": "入院记录", "matched": True, "reason": "主诉和现病史包含背痛"},
            {"file": "诊断查询", "matched": False, "reason": "未取得诊断查询接口数据"},
        ],
    }

    assert not _condition_explanation_matches_status(
        "未取得诊断查询接口数据，当前无法用该结构化数据源判断。",
        info,
    )
    assert _condition_explanation_matches_status("入院记录的主诉和现病史均包含背痛。", info)


def test_matched_condition_explanation_uses_document_match_when_diagnosis_is_unavailable():
    info = {
        "matched": True,
        "status": "MATCHED",
        "判断状态": "符合",
        "files": [
            {
                "file": "诊断查询",
                "证据角色": "主证据",
                "matched": False,
                "status": "UNKNOWN",
                "reason": "未取得诊断查询接口数据，当前无法用该结构化数据源判断",
            },
            {
                "file": "入院记录",
                "证据角色": "辅助证据",
                "matched": True,
                "status": "MATCHED",
                "reason": "主诉和现病史明确提及胸背部疼痛",
            },
            {
                "file": "出院记录",
                "证据角色": "辅助证据",
                "matched": True,
                "status": "MATCHED",
                "reason": "入院情况明确提及胸背部疼痛",
            },
            {
                "file": "手术记录",
                "证据角色": "辅助证据",
                "matched": False,
                "status": "NOT_MENTIONED",
                "reason": "未找到与背痛构成同一局部语义片段的文本",
            },
        ],
    }

    text = _condition_fallback_explanation(info)

    assert "胸背部疼痛" in text
    assert "未取得诊断查询接口数据" not in text
    assert "未找到与背痛" not in text


def test_matched_overall_rejects_failure_only_llm_explanation():
    first = {
        'matched': True,
        '判断状态': '符合',
        'per_condition': {
            '住院天数小于5天': {
                'matched': True,
                'files': [
                    {'file': '出院记录', 'matched': True, 'reason': '住院2天，小于5天'},
                    {'file': '就诊信息查询', 'matched': False, 'reason': '未取得接口数据'},
                ],
            },
            '背痛': {
                'matched': True,
                'files': [
                    {'file': '入院记录', 'matched': True, 'reason': '现病史包含背痛'},
                    {'file': '诊断查询', 'matched': False, 'reason': '未取得接口数据'},
                ],
            },
        },
    }

    assert not _overall_explanation_matches_status(
        '总体判断：符合。两个条件均未取得结构化接口数据，当前无法用该数据源判断。',
        first,
    )
    assert _overall_explanation_matches_status(
        '总体判断：符合。出院记录显示住院2天，入院记录现病史包含背痛。',
        first,
    )
    assert not _overall_explanation_matches_status(
        '总体判断：不符合。出院记录显示住院2天，入院记录现病史包含背痛。',
        first,
    )


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


def test_condition_fallback_explanation_describes_not_mentioned_source_result():
    info = {
        "matched": False,
        "status": "NOT_MENTIONED",
        "判断状态": "未提及",
        "reason": "诊断数据源查询成功，但未找到高血压记录",
        "files": [
            {
                "file": "诊断查询 (6条)",
                "证据角色": "主证据",
                "matched": False,
                "status": "NOT_MENTIONED",
                "reason": "共查询6条诊断记录，未找到与高血压匹配的记录",
            },
            {
                "file": "病历文档",
                "matched": False,
                "status": "UNKNOWN",
                "reason": "外部数据源调用失败",
            },
        ],
    }

    text = _condition_fallback_explanation(info)

    assert text.startswith("该条件判定为未提及。")
    assert "未找到与高血压匹配的记录" in text
    assert "外部数据源调用失败" not in text


def test_prompt_payload_keeps_source_level_not_mentioned_status():
    result = {
        "判断状态": "未提及",
        "results": [
            {
                "判断状态": "未提及",
                "per_condition": {
                    "有过高血压": {
                        "status": "NOT_MENTIONED",
                        "matched": False,
                        "reason": "未找到高血压",
                        "files": [
                            {
                                "file": "诊断查询",
                                "status": "NOT_MENTIONED",
                                "matched": False,
                                "reason": "查询成功但未找到目标诊断",
                            }
                        ],
                    }
                },
            }
        ],
    }

    payload = _build_payload(result)

    assert payload["子条件"][0]["判断状态"] == "未提及"
    assert payload["子条件"][0]["证据源"][0]["判断状态"] == "未提及"


def _canonical_explanation_result() -> dict:
    condition_result = {
        "condition_id": "c1",
        "condition": "术前48小时内白细胞>1.5×10^9/L",
        "status": "MATCHED",
        "matched": True,
        "reason_code": "MATCH_CONFIRMED",
        "reason": "检验结果4.0×10^9/L高于1.5×10^9/L，且检测时间在目标时间范围内",
        "data_quality": "COMPLETE",
        "conflict_level": "NONE",
        "source_decisions": [
            {
                "source_id": "service:lab-results",
                "source_name": "检验指标查询",
                "source_type": "service",
                "source_role": "PRIMARY",
                "status": "MATCHED",
                "reason_code": "MATCH_CONFIRMED",
                "data_quality": "COMPLETE",
                "evidence_count": 1,
                "record_ids": ["lab-1"],
                "reason": "检验结果满足条件",
                "selection_complete": True,
            }
        ],
        "evidence": [
            {
                "condition_id": "c1",
                "source_type": "service",
                "source_name": "检验指标查询",
                "source_role": "PRIMARY",
                "record_id": "lab-1",
                "entity": "白细胞",
                "event_time": "2026-06-05 10:00:00",
                "value": 4.0,
                "unit": "×10^9/L",
                "abnormal_flag": "H",
                "reference_range": "3.5-9.5",
                "status": "MATCHED",
                "reason_code": "MATCH_CONFIRMED",
                "data_quality": "COMPLETE",
                "reason": "结果满足条件",
                "metadata": {
                    "logical_source_id": "service:lab-results",
                    "in_time_window": True,
                    "value_satisfied": True,
                    "selection_complete": True,
                },
            }
        ],
    }
    return {
        "condition": "术前48小时内白细胞>1.5×10^9/L",
        "判断状态": "符合",
        "results": [
            {
                "判断状态": "符合",
                "matched": True,
                "reason": "总体符合",
                "per_condition": {
                    "术前48小时内白细胞>1.5×10^9/L": {
                        "status": "NOT_MATCHED",
                        "matched": False,
                        "判断状态": "不符合",
                        "reason": "旧字段错误地判定为不符合",
                        "condition_result": condition_result,
                        "files": [
                            {
                                "file": "检验指标查询 (1条)",
                                "logical_source_id": "service:lab-results",
                                "status": "NOT_MATCHED",
                                "matched": False,
                                "判断状态": "不符合",
                                "reason": "旧来源字段错误地判定为不符合",
                                "证据角色": "主证据",
                            }
                        ],
                    }
                },
            }
        ],
    }


def test_prompt_payload_uses_canonical_condition_and_source_decisions():
    payload = _build_payload(_canonical_explanation_result())
    condition = payload["子条件"][0]
    source = condition["证据源"][0]

    assert condition["判断状态"] == "符合"
    assert condition["判定依据"].startswith("检验结果4.0")
    assert source["判断状态"] == "符合"
    assert source["是否支持条件"] is True
    assert source["判定依据"] == "检验结果满足条件"


def test_prompt_payload_contains_adjudicated_fact_fields():
    payload = _build_payload(_canonical_explanation_result())
    facts = payload["子条件"][0]["裁决事实"]
    source_decision = facts["来源决策"][0]
    evidence = facts["证据事实"][0]

    assert facts["原因码"] == "MATCH_CONFIRMED"
    assert source_decision["status"] == "MATCHED"
    assert source_decision["selection_complete"] is True
    assert evidence["事件时间"] == "2026-06-05 10:00:00"
    assert evidence["数值"] == 4.0
    assert evidence["单位"] == "×10^9/L"
    assert evidence["扩展事实"]["selection_complete"] is True


class _FakeExplanationClient:
    response = "{}"

    def __init__(self, *args, **kwargs):
        pass

    def chat(self, *args, **kwargs):
        return self.response


def _polish_with_response(monkeypatch, response: dict) -> dict:
    monkeypatch.setenv("MEDICAL_QUERY_POLISH", "1")
    _FakeExplanationClient.response = json.dumps(response, ensure_ascii=False)
    monkeypatch.setattr("microharness.ollama.OllamaClient", _FakeExplanationClient)
    return polish_response_explanations(_canonical_explanation_result())


def test_polisher_rejects_hallucinated_date_and_value(monkeypatch):
    result = _polish_with_response(monkeypatch, {
        "总体解释": "",
        "子条件解释": {
            "C1": "该条件判定为符合。2026-06-04 10:00:00检测结果为5.0×10^9/L。",
        },
        "证据解释": {},
    })

    text = result["results"][0]["per_condition"]["术前48小时内白细胞>1.5×10^9/L"]["用户解释"]
    assert "2026-06-04" not in text
    assert "5.0×10^9/L" not in text
    assert "检验结果4.0×10^9/L" in text


def test_polisher_rejects_wrong_four_state_claim(monkeypatch):
    result = _polish_with_response(monkeypatch, {
        "总体解释": "",
        "子条件解释": {
            "C1": "该条件判定为不符合。检验结果4.0×10^9/L高于1.5×10^9/L。",
        },
        "证据解释": {},
    })

    text = result["results"][0]["per_condition"]["术前48小时内白细胞>1.5×10^9/L"]["用户解释"]
    assert text.startswith("该条件判定为符合。")


def test_polisher_accepts_fact_consistent_explanation(monkeypatch):
    expected = "该条件判定为符合。2026-06-05 10:00:00检测到白细胞结果4×10^9/L，高于1.5×10^9/L。"
    result = _polish_with_response(monkeypatch, {
        "总体解释": "",
        "子条件解释": {"C1": expected},
        "证据解释": {},
    })

    text = result["results"][0]["per_condition"]["术前48小时内白细胞>1.5×10^9/L"]["用户解释"]
    assert text == expected


def _future_skill_explanation_result() -> dict:
    condition = "未来风险评分>10"
    condition_result = {
        "condition_id": "future-condition-1",
        "condition": condition,
        "status": "MATCHED",
        "matched": True,
        "reason_code": "MATCH_CONFIRMED",
        "reason": "评分结果12.5，高于阈值10，记录可用于本次判定",
        "data_quality": "COMPLETE",
        "conflict_level": "NONE",
        "source_decisions": [
            {
                "source_id": "service:future-risk-score",
                "source_name": "未来风险评分查询",
                "source_type": "service",
                "source_role": "PRIMARY",
                "status": "MATCHED",
                "reason_code": "MATCH_CONFIRMED",
                "data_quality": "COMPLETE",
                "record_ids": ["future-7"],
                "reason": "未来评分记录满足条件",
                "selection_complete": True,
                "calibration_bucket": "high-risk",
            }
        ],
        "evidence": [
            {
                "condition_id": "future-condition-1",
                "source_type": "service",
                "source_name": "未来风险评分查询",
                "source_role": "PRIMARY",
                "record_id": "future-7",
                "entity": "未来风险评分",
                "value": 12.5,
                "unit": "score",
                "abnormal_flag": "X",
                "reference_range": "0-10",
                "status": "MATCHED",
                "reason_code": "MATCH_CONFIRMED",
                "reason": "模型输出原始评分12.5",
                "metadata": {
                    "logical_source_id": "service:future-risk-score",
                    "algorithm_version": "v3",
                    "custom_dimensions": {"cohort": "A"},
                    "nested_dimensions": {
                        "visible": {"label": "kept", "_secret": "hidden"},
                    },
                    "_private_trace": "hidden",
                },
            }
        ],
    }
    return {
        "condition": condition,
        "判断状态": "符合",
        "results": [
            {
                "判断状态": "符合",
                "matched": True,
                "condition_results": [condition_result],
                "per_condition": {
                    condition: {
                        "condition_result": condition_result,
                        "files": [
                            {
                                "file": "未来风险评分查询 (1条)",
                                "logical_source_id": "service:future-risk-score",
                                "证据角色": "主证据",
                                "matched": True,
                                "status": "MATCHED",
                                "reason": "未来评分记录满足条件",
                            }
                        ],
                    }
                },
            }
        ],
    }


def _polish_future_skill(monkeypatch, condition_text: str, source_text: str = "") -> dict:
    monkeypatch.setenv("MEDICAL_QUERY_POLISH", "1")
    _FakeExplanationClient.response = json.dumps({
        "总体解释": "",
        "子条件解释": {"C1": condition_text},
        "证据解释": {"C1F1": source_text} if source_text else {},
    }, ensure_ascii=False)
    monkeypatch.setattr("microharness.ollama.OllamaClient", _FakeExplanationClient)
    return polish_response_explanations(_future_skill_explanation_result())


def test_future_skill_public_metadata_flows_without_skill_specific_code():
    payload = _build_payload(_future_skill_explanation_result())
    facts = payload["子条件"][0]["裁决事实"]
    decision = facts["来源决策"][0]
    metadata = facts["证据事实"][0]["扩展事实"]

    assert decision["calibration_bucket"] == "high-risk"
    assert metadata["algorithm_version"] == "v3"
    assert metadata["custom_dimensions"] == {"cohort": "A"}
    assert metadata["nested_dimensions"] == {"visible": {"label": "kept"}}
    assert "_private_trace" not in metadata


def test_future_skill_fact_consistent_explanations_are_accepted_and_audited(monkeypatch):
    text = (
        "该条件判定为符合。来源为未来风险评分查询，记录ID为future-7，"
        "评分结果为12.5，单位为score，异常标志为X，参考范围为0-10。"
    )
    result = _polish_future_skill(monkeypatch, text, text)
    first = result["results"][0]
    info = first["per_condition"]["未来风险评分>10"]
    source = info["files"][0]

    assert info["用户解释"] == text
    assert info["解释校验"] == {
        "scope": "condition",
        "accepted": True,
        "used_fallback": False,
        "reason_codes": [],
    }
    assert source["用户解释"] == text
    assert source["解释校验"]["accepted"] is True
    assert first["解释校验"]["reason_codes"] == ["LLM_EXPLANATION_MISSING"]


@pytest.mark.parametrize(("claim", "reason_code"), [
    ("来源为另一风险评分服务", "SOURCE_NOT_IN_EVIDENCE"),
    ("记录ID为future-999", "RECORD_ID_NOT_IN_EVIDENCE"),
    ("单位为points", "UNIT_NOT_IN_EVIDENCE"),
    ("异常标志为Y", "ABNORMAL_FLAG_NOT_IN_EVIDENCE"),
    ("参考范围为0-20", "REFERENCE_RANGE_NOT_IN_EVIDENCE"),
    ("证据原文为“系统确认患者属于极高危人群”", "EVIDENCE_QUOTE_NOT_IN_FACTS"),
])
def test_future_skill_tampered_explicit_facts_are_rejected_and_audited(
    monkeypatch,
    claim,
    reason_code,
):
    candidate = f"该条件判定为符合。评分结果为12.5，高于阈值10；{claim}。"
    result = _polish_future_skill(monkeypatch, candidate)
    info = result["results"][0]["per_condition"]["未来风险评分>10"]

    assert info["用户解释"] != candidate
    assert "评分结果12.5" in info["用户解释"]
    assert info["解释校验"]["accepted"] is False
    assert info["解释校验"]["used_fallback"] is True
    assert reason_code in info["解释校验"]["reason_codes"]


def _compound_explanation_result(connector="and") -> dict:
    conditions = [
        {
            "condition_id": "c1",
            "condition": "40岁以上",
            "status": "MATCHED",
            "matched": True,
            "reason_code": "MATCH_CONFIRMED",
            "reason": "患者年龄45岁，满足40岁以上条件",
            "data_quality": "COMPLETE",
            "conflict_level": "NONE",
            "source_decisions": [],
            "evidence": [],
        },
        {
            "condition_id": "c2",
            "condition": "背痛",
            "status": "NOT_MENTIONED",
            "matched": False,
            "reason_code": "ENTITY_NOT_MENTIONED",
            "reason": "完整候选病历中未提及背痛",
            "data_quality": "COMPLETE",
            "conflict_level": "NONE",
            "source_decisions": [],
            "evidence": [],
        },
    ]
    return {
        "condition": "40岁以上并且背痛" if connector == "and" else "40岁以上或者背痛",
        "判断状态": "符合",
        "查询IR": {"连接关系": connector},
        "results": [
            {
                "判断状态": "符合",
                "matched": True,
                "reason": "旧总体字段错误地判定为符合",
                "condition_results": conditions,
                "per_condition": {
                    "40岁以上": {
                        "condition_result": conditions[0],
                        "判断状态": "不符合",
                        "matched": False,
                        "reason": "旧条件字段错误",
                        "files": [],
                    },
                    "背痛": {
                        "condition_result": conditions[1],
                        "判断状态": "符合",
                        "matched": True,
                        "reason": "旧条件字段错误",
                        "files": [],
                    },
                },
            }
        ],
    }


def test_overall_payload_uses_canonical_conditions_and_connector():
    payload = _build_payload(_compound_explanation_result("and"))

    assert payload["总体判断"] == "未提及"
    assert payload["总体裁决事实"]["连接关系"] == "AND"
    assert [item["判断状态"] for item in payload["总体裁决事实"]["子条件"]] == ["符合", "未提及"]
    assert payload["子条件"][0]["判断状态"] == "符合"
    assert payload["子条件"][1]["判断状态"] == "未提及"


def test_overall_validator_rejects_wrong_connector_and_missing_condition():
    result = _compound_explanation_result("and")
    first = result["results"][0]

    assert not _overall_explanation_matches_status(
        "总体判断：未提及。任一条件满足即可。条件1为符合，条件2为未提及。",
        first,
        result,
    )
    assert not _overall_explanation_matches_status(
        "总体判断：未提及。条件1年龄为45岁，符合要求。",
        first,
        result,
    )
    assert _overall_explanation_matches_status(
        "总体判断：未提及。全部条件需同时满足；条件1为符合，条件2为未提及。",
        first,
        result,
    )


def test_overall_validator_uses_or_semantics():
    result = _compound_explanation_result("or")
    first = result["results"][0]

    assert not _overall_explanation_matches_status(
        "总体判断：符合。全部条件需同时满足；条件1为符合，条件2为未提及。",
        first,
        result,
    )
    assert _overall_explanation_matches_status(
        "总体判断：符合。任一条件满足即可；条件1为符合，条件2为未提及。",
        first,
        result,
    )


def test_polisher_rejects_wrong_compound_semantics(monkeypatch):
    monkeypatch.setenv("MEDICAL_QUERY_POLISH", "1")
    _FakeExplanationClient.response = json.dumps({
        "总体解释": "总体判断：未提及。任一条件满足即可。条件1为符合，条件2为未提及。",
        "子条件解释": {},
        "证据解释": {},
    }, ensure_ascii=False)
    monkeypatch.setattr("microharness.ollama.OllamaClient", _FakeExplanationClient)

    result = polish_response_explanations(_compound_explanation_result("and"))
    text = result["results"][0]["用户解释"]

    assert "按AND关系" in text
    assert "任一条件满足即可" not in text


def test_polisher_accepts_fact_consistent_compound_explanation(monkeypatch):
    expected = "总体判断：符合。任一条件满足即可；条件1为符合，条件2为未提及。"
    monkeypatch.setenv("MEDICAL_QUERY_POLISH", "1")
    _FakeExplanationClient.response = json.dumps({
        "总体解释": expected,
        "子条件解释": {},
        "证据解释": {},
    }, ensure_ascii=False)
    monkeypatch.setattr("microharness.ollama.OllamaClient", _FakeExplanationClient)

    result = polish_response_explanations(_compound_explanation_result("or"))

    assert result["results"][0]["用户解释"] == expected


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
