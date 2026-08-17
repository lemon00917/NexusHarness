from __future__ import annotations

from copy import deepcopy

from scripts.evaluate_medical_filter import (
    build_review_manifest,
    evaluate_case,
    response_review_fingerprint,
    summarize,
    validate_gold,
)


def _condition_result(
    condition_id: str,
    condition: str,
    source_id: str,
    source_name: str,
    record_id: str,
) -> dict:
    return {
        "condition_id": condition_id,
        "condition": condition,
        "status": "MATCHED",
        "matched": True,
        "reason_code": "MATCH_CONFIRMED",
        "reason": f"{source_name}中的规范证据满足条件",
        "data_quality": "COMPLETE",
        "source_decisions": [
            {
                "source_id": source_id,
                "source_name": source_name,
                "source_type": "service",
                "status": "MATCHED",
                "reason_code": "MATCH_CONFIRMED",
                "data_quality": "COMPLETE",
                "record_ids": [record_id],
            }
        ],
        "evidence": [
            {
                "condition_id": condition_id,
                "source_type": "service",
                "source_name": source_name,
                "record_id": record_id,
                "entity": condition,
                "event_time": "2026-07-01 10:00:00",
                "value": 12.5,
                "unit": "score",
                "status": "MATCHED",
                "reason_code": "MATCH_CONFIRMED",
                "data_quality": "COMPLETE",
                "metadata": {
                    "logical_source_id": source_id,
                    "in_time_window": True,
                    "selection_complete": True,
                },
            }
        ],
    }


def _response() -> dict:
    first_condition = _condition_result(
        "c1",
        "评估前7天内未来风险评分>10",
        "service:future-risk-score",
        "未来风险评分查询",
        "future-7",
    )
    second_condition = _condition_result(
        "c2",
        "未来资格存在",
        "service:future-eligibility",
        "未来资格查询",
        "eligibility-3",
    )
    return {
        "判断状态": "符合",
        "查询归一化": {
            "规范问题": "评估前7天内未来风险评分>10并且未来资格存在",
            "来源": "llm+validator",
            "是否需要复核": False,
        },
        "查询IR": {
            "原始条件": "评估前7天内未来风险评分>10并且未来资格存在",
            "类型": "compound",
            "连接关系": "and",
            "子条件": [
                {
                    "条件ID": "c1",
                    "条件文本": first_condition["condition"],
                    "领域": "future_risk",
                    "谓词": "greater_than",
                    "目标服务": ["future-risk-score"],
                    "evidence_plan_source_ids": ["c1:service:future-risk-score"],
                    "数值比较": {
                        "subject": "未来风险评分",
                        "operator": ">",
                        "threshold": 10.0,
                        "unit": "score",
                    },
                    "时间约束": {
                        "范围": "事件前",
                        "事件": "评估",
                        "关系": "before",
                        "时长": 7.0,
                        "单位": "天",
                    },
                },
                {
                    "条件ID": "c2",
                    "条件文本": second_condition["condition"],
                    "领域": "future_eligibility",
                    "谓词": "exists",
                    "目标服务": ["future-eligibility"],
                    "evidence_plan_source_ids": ["c2:service:future-eligibility"],
                },
            ],
        },
        "证据计划": {
            "conditions": [
                {
                    "condition_id": "c1",
                    "sources": [
                        {
                            "source_id": "c1:service:future-risk-score",
                            "source_type": "service",
                            "resolution_status": "resolved",
                            "resolved_name": "future-risk-score",
                        }
                    ],
                },
                {
                    "condition_id": "c2",
                    "sources": [
                        {
                            "source_id": "c2:service:future-eligibility",
                            "source_type": "service",
                            "resolution_status": "resolved",
                            "resolved_name": "future-eligibility",
                        }
                    ],
                },
            ]
        },
        "results": [
            {
                "判断状态": "符合",
                "matched": True,
                "reason": "两个规范子条件均符合。",
                "解释校验": {
                    "scope": "overall",
                    "accepted": True,
                    "used_fallback": False,
                    "reason_codes": [],
                },
                "overall_result": {
                    "connector": "AND",
                    "status": "MATCHED",
                    "matched": True,
                    "reason_code": "ALL_CONDITIONS_MATCHED",
                },
                "condition_results": [first_condition, second_condition],
                "per_condition": {
                    first_condition["condition"]: {
                        "condition_result": first_condition,
                        "解释校验": {
                            "scope": "condition",
                            "accepted": True,
                            "used_fallback": False,
                            "reason_codes": [],
                        },
                        "files": [
                            {
                                "file": "未来风险评分查询 (1条)",
                                "logical_source_id": "service:future-risk-score",
                                "解释校验": {
                                    "scope": "source",
                                    "accepted": True,
                                    "used_fallback": False,
                                    "reason_codes": [],
                                },
                            }
                        ],
                    },
                    second_condition["condition"]: {
                        "condition_result": second_condition,
                        "files": [],
                    },
                },
            }
        ],
    }


def _case() -> dict:
    return {
        "id": "future_skill_layered_contract",
        "title": "未来 skill 分层评测契约",
        "category": ["future", "numeric"],
        "review": {
            "status": "verified",
            "reviewed_by": "test-suite",
            "reviewed_at": "2026-07-28T00:00:00+08:00",
            "note": "Synthetic response with manually fixed expected facts.",
        },
        "expected": {
            "overall_status": "符合",
            "condition_count": 2,
            "required_condition_contains": ["未来风险评分", "未来资格"],
            "query_type": "compound",
            "connector": "and",
            "normalization": {
                "来源": "llm+validator",
                "是否需要复核": False,
            },
            "condition_ir": [
                {
                    "condition_id": "c1",
                    "fields": {
                        "领域": "future_risk",
                        "谓词": "greater_than",
                        "数值比较.operator": ">",
                        "数值比较.threshold": 10.0,
                    },
                },
                {
                    "condition_id": "c2",
                    "fields": {"领域": "future_eligibility", "谓词": "exists"},
                },
            ],
            "required_source_ids": [
                "c1:service:future-risk-score",
                "c2:service:future-eligibility",
            ],
            "routing_assertions": [
                {
                    "condition_id": "c1",
                    "source_id": "c1:service:future-risk-score",
                    "fields": {
                        "resolution_status": "resolved",
                        "resolved_name": "future-risk-score",
                    },
                }
            ],
            "evidence_assertions": [
                {
                    "condition_id": "c1",
                    "min_count": 1,
                    "required_source_ids": ["service:future-risk-score"],
                    "required_record_ids": ["future-7"],
                    "fields_any": {
                        "unit": "score",
                        "metadata.selection_complete": True,
                    },
                }
            ],
            "temporal_assertions": [
                {
                    "condition_id": "c1",
                    "fields": {
                        "事件": "评估",
                        "关系": "before",
                        "时长": 7.0,
                        "单位": "天",
                    },
                    "evidence_fields_any": {"in_time_window": True},
                }
            ],
            "condition_statuses": [
                {
                    "condition_id": "c1",
                    "status": "MATCHED",
                    "reason_code": "MATCH_CONFIRMED",
                    "data_quality": "COMPLETE",
                },
                {"condition_id": "c2", "status": "MATCHED"},
            ],
            "overall_result": {
                "connector": "AND",
                "status": "MATCHED",
                "reason_code": "ALL_CONDITIONS_MATCHED",
            },
            "explanation_audits": [
                {"scope": "overall", "fields": {"accepted": True, "used_fallback": False}},
                {
                    "scope": "condition",
                    "condition_id": "c1",
                    "fields": {"accepted": True, "used_fallback": False},
                },
                {
                    "scope": "source",
                    "condition_id": "c1",
                    "source_id": "service:future-risk-score",
                    "fields": {"accepted": True, "used_fallback": False},
                },
            ],
        },
    }


def test_layered_evaluator_scores_future_skill_without_skill_specific_logic():
    result = evaluate_case(_case(), _response(), 42.0)

    assert result["status"] == "PASS"
    assert result["first_failure_layer"] is None
    assert result["first_blocked_layer"] is None
    assert all(layer["status"] == "PASS" for layer in result["layers"].values())
    assert result["observed"]["planned_source_ids"] == [
        "c1:service:future-risk-score",
        "future-risk-score",
        "service:future-risk-score",
        "c2:service:future-eligibility",
        "future-eligibility",
        "service:future-eligibility",
    ]


def test_layered_evaluator_attributes_to_earliest_failed_layer():
    case = _case()
    response = _response()
    response["查询IR"]["子条件"][0]["领域"] = "wrong_domain"

    result = evaluate_case(case, response, None)

    assert result["status"] == "FAIL"
    assert result["first_failure_layer"] == "ir"
    assert result["layers"]["understanding"]["status"] == "PASS"
    assert result["layers"]["ir"]["failure_codes"] == ["IR_FIELD_MISMATCH"]


def test_data_unavailability_is_blocked_at_condition_adjudication_layer():
    case = {
        "id": "future_source_blocked",
        "review": {
            "status": "verified",
            "reviewed_by": "test-suite",
            "reviewed_at": "2026-07-28T00:00:00+08:00",
            "note": "Synthetic blocked-source adjudication case.",
        },
        "expected": {
            "condition_count": 2,
            "required_condition_contains": ["未来风险评分", "未来资格"],
            "condition_statuses": [{"condition_id": "c1", "status": "MATCHED"}],
        },
    }
    response = _response()
    result_item = response["results"][0]["condition_results"][0]
    result_item["status"] = "UNKNOWN"
    result_item["reason"] = "数据源不可用，当前无法判断"

    result = evaluate_case(case, response, None)

    assert result["status"] == "BLOCKED"
    assert result["first_failure_layer"] is None
    assert result["first_blocked_layer"] == "condition_adjudication"
    assert result["blocked_codes"] == ["CONDITION_STATUS_MISMATCH"]


def test_summary_aggregates_layer_and_failure_attribution_metrics():
    passed = evaluate_case(_case(), _response(), 10.0)
    failed_response = _response()
    failed_response["查询IR"]["子条件"][0]["谓词"] = "wrong_predicate"
    failed = evaluate_case(_case(), failed_response, 20.0)

    summary = summarize([passed, failed])

    assert summary["layer_metrics"]["ir"]["cases"] == {
        "PASS": 1,
        "FAIL": 1,
        "BLOCKED": 0,
        "NOT_EVALUATED": 0,
    }
    assert summary["first_failure_layer_counts"] == {"ir": 1}
    assert summary["failure_code_counts"] == {"IR_FIELD_MISMATCH": 1}


def test_gold_schema_accepts_future_skill_contract():
    assert validate_gold({"cases": [_case()]}) == []


def test_gold_schema_rejects_duplicate_case_ids_and_invalid_fields():
    invalid = _case()
    invalid["expected"]["condition_ir"][0]["fields"] = ["not", "an", "object"]

    errors = validate_gold({"cases": [invalid, deepcopy(invalid)]})

    assert any("expected object" in error for error in errors)
    assert any("duplicate case id" in error for error in errors)


def test_gold_schema_rejects_ambiguous_selectors_and_invalid_audit_scope():
    invalid = _case()
    invalid["expected"]["routing_assertions"] = [{"condition_id": "c1", "fields": {}}]
    invalid["expected"]["evidence_assertions"] = [{"min_count": 1}]
    invalid["expected"]["explanation_audits"] = [
        {"scope": "record", "fields": {"accepted": True}},
        {"scope": "source", "condition_id": "c1", "fields": {}},
    ]

    errors = validate_gold({"cases": [invalid]})

    assert any("source_id or source_contains is required" in error for error in errors)
    assert any("condition_id or condition_contains is required" in error for error in errors)
    assert any("invalid scope 'record'" in error for error in errors)


def test_gold_schema_requires_verified_provenance_for_clinical_expectations():
    case = _case()
    case["review"] = {"status": "routing_only", "note": "Only routing was reviewed."}

    errors = validate_gold({"cases": [case]})

    assert any("verified is required for clinical expectations" in error for error in errors)

    case["review"] = {"status": "verified", "note": "Reviewed."}
    errors = validate_gold({"cases": [case]})
    assert any("reviewed_by" in error for error in errors)
    assert any("reviewed_at" in error for error in errors)


def test_response_fingerprint_ignores_runtime_trace_and_timing_noise():
    response = _response()
    first_hash = response_review_fingerprint(response)
    response["request_trace"] = {"request_id": "different", "timings": {"total_ms": 999}}
    response["timings"] = {"total_ms": 1234}

    assert response_review_fingerprint(response) == first_hash


def test_stale_review_blocks_clinical_scoring_without_hiding_routing_results():
    case = _case()
    case["review"]["source_response_sha256"] = "0" * 64

    result = evaluate_case(case, _response(), None)

    assert result["status"] == "BLOCKED"
    assert result["review"]["binding_status"] == "stale"
    clinical = [item for item in result["assertions"] if item["group"] == "clinical"]
    routing = [item for item in result["assertions"] if item["group"] == "routing"]
    assert clinical
    assert all(item["outcome"] == "BLOCKED" for item in clinical)
    assert all(item["code"] == "REVIEW_RESPONSE_DRIFT" for item in clinical)
    assert all(item["outcome"] == "PASS" for item in routing)


def test_summary_reports_bound_review_segments_and_request_trace_metrics():
    response = _response()
    response["request_trace"] = {
        "schema_version": "1.1.0",
        "lifecycle_status": "completed",
        "outcome": {"status": "MATCHED"},
        "models": {"router": "future-router", "judge": "future-judge"},
        "queue": {"wait_ms": 25},
        "timings": {"understanding_ms": 30, "total_ms": 80},
        "bottleneck": {"stage": "understanding_ms", "elapsed_ms": 30},
        "sources": {
            "unavailable_count": 0,
            "degraded_count": 1,
            "unknown_uncertainty_kind_counts": {
                "REJECTED_CANDIDATE": 2,
                "INCOMPLETE_SEARCH": 1,
            },
        },
        "explanations": {"fallback_count": 1},
        "issues": [{"layer": "evidence", "code": "PARTIAL_SOURCE", "count": 1}],
        "first_issue": {"layer": "evidence", "code": "PARTIAL_SOURCE", "count": 1},
    }
    case = _case()
    case["review"]["source_response_sha256"] = response_review_fingerprint(response)
    result = evaluate_case(case, response, 80.0)

    summary = summarize([result])

    assert summary["review_metrics"]["bound_clinical_cases"] == 1
    assert summary["review_metrics"]["bound_clinical_accuracy"]["failed"] == 0
    assert summary["trace_metrics"]["coverage"]["rate"] == 1.0
    assert summary["trace_metrics"]["coverage"]["native_cases"] == 1
    assert summary["trace_metrics"]["coverage"]["legacy_synthesized_cases"] == 0
    assert summary["trace_metrics"]["source_degraded_cases"] == 1
    assert summary["trace_metrics"]["source_uncertainty_kind_counts"] == {
        "INCOMPLETE_SEARCH": 1,
        "REJECTED_CANDIDATE": 2,
    }
    assert summary["trace_metrics"]["source_uncertainty_case_counts"] == {
        "INCOMPLETE_SEARCH": 1,
        "REJECTED_CANDIDATE": 1,
    }
    assert summary["trace_metrics"]["explanation_fallback_cases"] == 1
    assert summary["trace_metrics"]["stage_timings_ms"]["total_ms"]["p50"] == 80.0
    assert summary["segment_metrics"]["category"]["future"]["total"] == 1
    assert summary["segment_metrics"]["source_health"]["degraded"]["total"] == 1
    assert summary["segment_metrics"]["source_uncertainty"]["INCOMPLETE_SEARCH"]["total"] == 1
    assert summary["segment_metrics"]["source_uncertainty"]["REJECTED_CANDIDATE"]["total"] == 1
    assert summary["segment_metrics"]["model"]["judge=future-judge"]["total"] == 1


def test_summary_synthesizes_trace_from_legacy_timings_without_claiming_native_trace():
    response = _response()
    response["request_id"] = "legacy-request"
    response["timings"] = {
        "understanding_ms": 30,
        "structured_services_ms": 75,
        "total_ms": 120,
    }
    result = evaluate_case(_case(), response, 120.0)

    summary = summarize([result])

    assert result["request_trace"]["origin"] == "legacy_synthesized"
    assert result["request_trace"]["bottleneck"] == {
        "stage": "structured_services_ms",
        "elapsed_ms": 75,
    }
    assert summary["trace_metrics"]["coverage"] == {
        "traced_cases": 1,
        "total_cases": 1,
        "rate": 1.0,
        "native_cases": 0,
        "legacy_synthesized_cases": 1,
        "native_rate": 0.0,
    }
    assert summary["trace_metrics"]["stage_timings_ms"]["total_ms"]["p50"] == 120.0


def test_review_manifest_creates_pending_template_without_changing_gold():
    case = _case()
    result = evaluate_case(case, _response(), None)

    manifest = build_review_manifest([case], [result])

    entry = manifest["cases"][0]
    assert entry["current_review"]["status"] == "verified"
    assert entry["review_template"]["status"] == "pending"
    assert entry["review_template"]["source_response_sha256"] == result["review"][
        "actual_response_sha256"
    ]
    assert case["review"]["status"] == "verified"
