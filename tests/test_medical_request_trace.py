import asyncio
import json
from concurrent.futures import ThreadPoolExecutor

from starlette.requests import Request

from microharness.medical.request_trace import (
    build_medical_query_trace,
    medical_query_trace_log,
)


def _json_request(payload: dict) -> Request:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    delivered = False

    async def receive():
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/medical/query",
            "raw_path": b"/api/medical/query",
            "query_string": b"",
            "headers": [(b"content-type", b"application/json")],
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
        },
        receive,
    )


def _future_response():
    condition_one = {
        "condition_id": "c1",
        "condition": "future temporal condition",
        "status": "UNKNOWN",
        "reason_code": "MISSING_EVENT_TIME",
        "data_quality": "SOURCE_ERROR",
        "conflict_level": "SUPPORTING_DISAGREEMENT",
        "source_decisions": [
            {
                "source_id": "service:future-imaging",
                "status": "UNKNOWN",
                "reason_code": "SOURCE_UNAVAILABLE",
                "data_quality": "SOURCE_ERROR",
                "selection_complete": True,
                "missing_capabilities": [],
            }
        ],
    }
    condition_two = {
        "condition_id": "c2",
        "condition": "future risk condition",
        "status": "MATCHED",
        "reason_code": "MATCH_CONFIRMED",
        "data_quality": "PARTIAL",
        "conflict_level": "NONE",
        "source_decisions": [
            {
                "source_id": "service:future-risk-score",
                "status": "MATCHED",
                "reason_code": "MISSING_REQUIRED_CAPABILITY",
                "data_quality": "PARTIAL",
                "selection_complete": False,
                "missing_capabilities": ["calibration"],
            }
        ],
    }
    return {
        "overall_result": {
            "status": "UNKNOWN",
            "reason_code": "CONDITION_UNKNOWN",
        },
        "IR\u8d28\u91cf": {
            "valid": True,
            "issues": [],
            "warnings": [{"code": "DOMAIN_UNCERTAIN"}],
        },
        "\u8bc1\u636e\u8ba1\u5212": {
            "unresolved_count": 1,
            "conditions": [],
        },
        "timings": {
            "normalization_ms": 100,
            "understanding_ms": 700,
            "condition_execution_ms": 350,
            "explanation_polish_ms": 1200,
            "total_ms": 2450,
        },
        "results": [
            {
                "overall_result": {
                    "status": "UNKNOWN",
                    "reason_code": "CONDITION_UNKNOWN",
                },
                "condition_results": [condition_one, condition_two],
                "\u89e3\u91ca\u6821\u9a8c": {
                    "scope": "overall",
                    "accepted": False,
                    "used_fallback": True,
                    "reason_codes": ["STATUS_MISMATCH"],
                },
                "per_condition": {
                    "future temporal condition": {
                        "condition_result": condition_one,
                        "\u89e3\u91ca\u6821\u9a8c": {
                            "scope": "condition",
                            "accepted": True,
                            "used_fallback": False,
                            "reason_codes": [],
                        },
                        "files": [
                            {
                                "logical_source_id": "service:future-imaging",
                                "\u89e3\u91ca\u6821\u9a8c": {
                                    "scope": "source",
                                    "accepted": False,
                                    "used_fallback": True,
                                    "reason_codes": ["CRITICAL_FACT_MISMATCH"],
                                },
                            }
                        ],
                    }
                },
            }
        ],
    }


def test_request_trace_aggregates_queue_timings_sources_and_audits():
    response = _future_response()
    trace = build_medical_query_trace(
        response,
        request_id="trace-1",
        admission={
            "submitted_at": 100.0,
            "started_at": 100.25,
            "max_concurrency": 3,
            "active_count": 1,
            "max_queue": 8,
            "queue_length": 0,
            "queue_position": 0,
            "queue_timeout_seconds": 45,
        },
        models={"router": "router-x", "judge": "judge-y", "planner": "planner-z"},
    )

    assert trace["schema_version"] == "1.1.0"
    assert trace["request_id"] == "trace-1"
    assert trace["queue"]["wait_ms"] == 250
    assert trace["bottleneck"] == {
        "stage": "explanation_polish_ms",
        "elapsed_ms": 1200,
    }
    assert trace["conditions"]["status_counts"] == {"MATCHED": 1, "UNKNOWN": 1}
    assert trace["conditions"]["conflict_count"] == 1
    assert trace["sources"]["total"] == 2
    assert trace["sources"]["unavailable_count"] == 1
    assert trace["sources"]["degraded_count"] == 2
    assert trace["sources"]["uncertainty_kind_counts"] == {
        "NONE": 1,
        "SOURCE_FAILURE": 1,
    }
    assert trace["sources"]["unknown_uncertainty_kind_counts"] == {
        "SOURCE_FAILURE": 1,
    }
    assert trace["explanations"]["total"] == 3
    assert trace["explanations"]["fallback_count"] == 2
    assert trace["explanations"]["reason_code_counts"] == {
        "CRITICAL_FACT_MISMATCH": 1,
        "STATUS_MISMATCH": 1,
    }
    assert trace["first_issue"] == {
        "layer": "ir",
        "code": "DOMAIN_UNCERTAIN",
        "count": 1,
    }
    assert trace["outcome"] == {
        "status": "UNKNOWN",
        "reason_code": "CONDITION_UNKNOWN",
        "data_quality": "SOURCE_ERROR",
        "conflict_level": "SUPPORTING_DISAGREEMENT",
    }


def test_failed_request_trace_is_machine_readable_without_a_medical_response():
    trace = build_medical_query_trace(
        {},
        request_id="failed-1",
        lifecycle_status="failed",
        error="database connection failed",
    )

    assert trace["lifecycle_status"] == "failed"
    assert trace["outcome"]["status"] == "UNKNOWN"
    assert any(item["code"] == "REQUEST_FAILED" for item in trace["issues"])
    assert trace["error"] == "database connection failed"


def test_trace_classifies_future_unknown_sources_without_known_skill_ids():
    response = {
        "overall_result": {"status": "UNKNOWN", "reason_code": "CONDITION_UNKNOWN"},
        "results": [
            {
                "condition_results": [
                    {
                        "condition_id": "c-future",
                        "condition": "future condition",
                        "status": "UNKNOWN",
                        "reason_code": "INSUFFICIENT_EVIDENCE",
                        "data_quality": "PARTIAL",
                        "conflict_level": "NONE",
                        "source_decisions": [
                            {
                                "source_id": "service:future-source-a",
                                "status": "UNKNOWN",
                                "reason_code": "INSUFFICIENT_EVIDENCE",
                                "data_quality": "PARTIAL",
                                "selection_complete": True,
                                "uncertainty_kind": "REJECTED_CANDIDATE",
                            },
                            {
                                "source_id": "service:future-source-b",
                                "status": "UNKNOWN",
                                "reason_code": "INSUFFICIENT_EVIDENCE",
                                "data_quality": "PARTIAL",
                                "selection_complete": False,
                                "uncertainty_kind": "REJECTED_CANDIDATE",
                            },
                        ],
                    }
                ]
            }
        ],
    }

    trace = build_medical_query_trace(response)

    assert trace["sources"]["unknown_uncertainty_kind_counts"] == {
        "INCOMPLETE_SEARCH": 1,
        "REJECTED_CANDIDATE": 1,
    }


def test_clean_future_skill_trace_does_not_require_known_service_ids():
    response = {
        "overall_result": {"status": "MATCHED", "reason_code": "SINGLE_CONDITION_RESULT"},
        "timings": {"condition_execution_ms": 9, "total_ms": 10},
        "results": [
            {
                "condition_results": [
                    {
                        "condition_id": "c1",
                        "status": "MATCHED",
                        "reason_code": "MATCH_CONFIRMED",
                        "data_quality": "COMPLETE",
                        "conflict_level": "NONE",
                        "source_decisions": [
                            {
                                "source_id": "service:future-genomics-v2",
                                "status": "MATCHED",
                                "reason_code": "MATCH_CONFIRMED",
                                "data_quality": "COMPLETE",
                                "selection_complete": True,
                                "missing_capabilities": [],
                            }
                        ],
                    }
                ]
            }
        ],
    }

    trace = build_medical_query_trace(response, request_id="future-1")

    assert trace["issues"] == []
    assert trace["sources"]["total"] == 1
    assert trace["outcome"]["status"] == "MATCHED"
    log_entry = medical_query_trace_log(trace)
    assert log_entry["source_count"] == 1
    assert log_entry["source_uncertainty"] == {}


def test_medical_query_endpoint_attaches_trace_on_success(monkeypatch):
    import importlib

    from microharness.medical.query_concurrency import MedicalQueryCoordinator

    web_app = importlib.import_module("web.app")
    coordinator = MedicalQueryCoordinator(max_concurrency=1, max_queue=1)
    executor = ThreadPoolExecutor(max_workers=1)
    monkeypatch.setattr(web_app, "_MEDICAL_QUERY_COORDINATOR", coordinator)
    monkeypatch.setattr(web_app, "_MEDICAL_QUERY_POOL", executor)
    monkeypatch.setattr(
        web_app,
        "_run_medical_query",
        lambda *args: {
            "overall_result": {
                "status": "MATCHED",
                "reason_code": "SINGLE_CONDITION_RESULT",
            },
            "timings": {"condition_execution_ms": 8, "total_ms": 10},
            "results": [
                {
                    "condition_results": [
                        {
                            "condition_id": "c1",
                            "status": "MATCHED",
                            "reason_code": "MATCH_CONFIRMED",
                            "data_quality": "COMPLETE",
                            "conflict_level": "NONE",
                            "source_decisions": [],
                        }
                    ]
                }
            ],
        },
    )

    payload = {
        "condition": "test condition",
        "register_no": "patient-1",
        "request_id": "endpoint-success",
        "router_model": "router-x",
        "judge_model": "judge-y",
        "planner_model": "planner-z",
    }
    try:
        result = asyncio.run(web_app.medical_query(_json_request(payload)))
    finally:
        executor.shutdown(wait=True)

    assert result["request_id"] == "endpoint-success"
    assert result["request_trace"]["lifecycle_status"] == "completed"
    assert result["request_trace"]["outcome"]["status"] == "MATCHED"
    assert result["request_trace"]["models"] == {
        "router": "router-x",
        "judge": "judge-y",
        "planner": "planner-z",
    }


def test_medical_query_endpoint_fills_single_visit_context(monkeypatch, tmp_path):
    import importlib

    from microharness.medical.query_concurrency import MedicalQueryCoordinator

    web_app = importlib.import_module("web.app")
    patient_dir = tmp_path / "0000000120"
    visit_dir = patient_dir / "174"
    visit_dir.mkdir(parents=True)
    (patient_dir / "_meta.json").write_text(
        json.dumps({"global_patient_id": "00001_120"}), encoding="utf-8"
    )
    (visit_dir / "_visit.json").write_text(
        json.dumps({"visit_no": "174", "global_visit_id": "00001_174"}),
        encoding="utf-8",
    )
    coordinator = MedicalQueryCoordinator(max_concurrency=1, max_queue=1)
    executor = ThreadPoolExecutor(max_workers=1)
    captured_args = []

    def fake_query(*args):
        captured_args.append(args)
        return {
            "overall_result": {"status": "MATCHED", "reason_code": "SINGLE_CONDITION_RESULT"},
            "results": [{"matched": True}],
        }

    monkeypatch.setattr(web_app, "_PATIENTS_DIR", tmp_path)
    monkeypatch.setattr(web_app, "_MEDICAL_QUERY_COORDINATOR", coordinator)
    monkeypatch.setattr(web_app, "_MEDICAL_QUERY_POOL", executor)
    monkeypatch.setattr(web_app, "_run_medical_query", fake_query)

    payload = {
        "condition": "test condition",
        "register_no": "0000000120",
        "visit_no": "",
        "global_patient_id": "",
        "global_visit_id": "",
        "request_id": "endpoint-visit-context",
    }
    try:
        result = asyncio.run(web_app.medical_query(_json_request(payload)))
    finally:
        executor.shutdown(wait=True)

    assert result["request_id"] == "endpoint-visit-context"
    assert captured_args[0][2:5] == ("174", "00001_120", "00001_174")


def test_medical_query_endpoint_attaches_trace_on_execution_failure(monkeypatch):
    import importlib

    from microharness.medical.query_concurrency import MedicalQueryCoordinator

    web_app = importlib.import_module("web.app")
    coordinator = MedicalQueryCoordinator(max_concurrency=1, max_queue=1)
    executor = ThreadPoolExecutor(max_workers=1)
    monkeypatch.setattr(web_app, "_MEDICAL_QUERY_COORDINATOR", coordinator)
    monkeypatch.setattr(web_app, "_MEDICAL_QUERY_POOL", executor)

    def fail_query(*args):
        raise RuntimeError("synthetic execution failure")

    monkeypatch.setattr(web_app, "_run_medical_query", fail_query)
    payload = {
        "condition": "test condition",
        "register_no": "patient-1",
        "request_id": "endpoint-failure",
    }
    try:
        result = asyncio.run(web_app.medical_query(_json_request(payload)))
    finally:
        executor.shutdown(wait=True)

    assert result["request_id"] == "endpoint-failure"
    assert result["request_trace"]["lifecycle_status"] == "failed"
    assert result["request_trace"]["error"] == "synthetic execution failure"
    assert any(
        issue["code"] == "REQUEST_FAILED"
        for issue in result["request_trace"]["issues"]
    )


def test_medical_query_endpoint_attaches_trace_when_queue_rejects(monkeypatch):
    import importlib

    from microharness.medical.query_concurrency import MedicalQueryCoordinator

    web_app = importlib.import_module("web.app")
    coordinator = MedicalQueryCoordinator(max_concurrency=1, max_queue=0)
    monkeypatch.setattr(web_app, "_MEDICAL_QUERY_COORDINATOR", coordinator)

    async def scenario():
        await coordinator.acquire("occupied")
        try:
            response = await web_app.medical_query(
                _json_request(
                    {
                        "condition": "test condition",
                        "register_no": "patient-1",
                        "request_id": "endpoint-rejected",
                    }
                )
            )
        finally:
            coordinator.release("occupied")
        return response

    response = asyncio.run(scenario())
    payload = json.loads(response.body)

    assert response.status_code == 429
    assert payload["request_id"] == "endpoint-rejected"
    assert payload["request_trace"]["lifecycle_status"] == "rejected"
    assert payload["request_trace"]["queue"]["max_concurrency"] == 1
    assert any(
        issue["code"] == "REQUEST_FAILED"
        for issue in payload["request_trace"]["issues"]
    )
