import json

from microharness.services.http_client import (
    _business_error,
    _merge_external_results,
    _normalize_response_records,
    call_service_as_binding,
)


def test_http_200_with_null_data_and_message_is_business_error():
    assert _business_error([{"msg": "请先登录系统", "data": None}]) == "请先登录系统"


def test_explicit_business_failure_is_error():
    assert _business_error([{"success": False, "message": "鉴权失败", "data": []}]) == "鉴权失败"


def test_http_200_with_failure_code_is_business_error():
    assert _business_error([{"code": 500, "msg": "数据库连接失败"}]) == "数据库连接失败"


def test_success_code_does_not_turn_records_into_business_error():
    assert _business_error([{"code": 200, "data": [{"name": "背痛"}]}]) == ""


def test_empty_successful_result_is_not_business_error():
    assert _business_error([{"success": True, "message": "查询成功", "data": []}]) == ""


def test_merge_external_results_resolves_record_identity_before_field_filtering():
    merged = _merge_external_results([
        {
            "file": "检验指标查询",
            "template": "lab-results",
            "service_id": "lab-results",
            "rec_prefix": "检验",
            "keep_fields": ["inspItemDesc"],
            "field_labels": {"inspItemDesc": "化验项目描述"},
            "semantic": {
                "presentation": {
                    "record_identity": {
                        "label": "检验报告号",
                        "fields": ["inspRptId", "hdcInspRptId"],
                    }
                }
            },
            "bindings": [
                {"html_field": "inspRptId", "value": ""},
                {"html_field": "hdcInspRptId", "value": "00001_2000000186||77"},
                {"html_field": "inspItemDesc", "value": "中性粒细胞数"},
            ],
        }
    ])

    assert len(merged["bindings"]) == 1
    binding = merged["bindings"][0]
    assert binding["record_id"] == "00001_2000000186||77"
    assert binding["record_id_label"] == "检验报告号"
    assert binding["record_id_field"] == "hdcInspRptId"
    assert "hdcInspRptId" not in binding["html_field"]


def test_merge_external_results_honors_declared_hospital_id_priority():
    merged = _merge_external_results([
        {
            "file": "诊断查询",
            "template": "diagnosis-query",
            "service_id": "diagnosis-query",
            "rec_prefix": "诊断",
            "keep_fields": ["diagnoseName"],
            "field_labels": {"diagnoseName": "诊断名称"},
            "semantic": {
                "presentation": {
                    "record_identity": {
                        "label": "诊断ID",
                        "fields": ["hosDiagId", "hdcDiagId"],
                    }
                }
            },
            "bindings": [
                {"html_field": "hdcDiagId", "value": "PLATFORM-DIAG-2"},
                {"html_field": "hosDiagId", "value": "00001_174||2"},
                {"html_field": "diagnoseName", "value": "背痛"},
            ],
        }
    ])

    binding = merged["bindings"][0]
    assert binding["record_id"] == "00001_174||2"
    assert binding["record_id_label"] == "诊断ID"
    assert binding["record_id_field"] == "hosDiagId"


def test_nested_records_response_is_normalized(capsys):
    rows = [{"diagnoseName": "背痛"}]

    assert _normalize_response_records([{"data": {"records": rows}}], "diagnosis-query") == rows
    assert "路径=data.records" in capsys.readouterr().out


def test_unrecognized_response_shape_logs_concrete_details(capsys):
    assert _normalize_response_records(
        [{"success": True, "data": "unexpected"}],
        "diagnosis-query",
    ) == []

    output = capsys.readouterr().out
    assert "[外部API][解析异常] diagnosis-query" in output
    assert "实际data类型=str" in output
    assert "外层keys=['success', 'data']" in output
    assert "响应摘要=" in output


def test_post_logs_complete_business_and_wire_parameters(monkeypatch, capsys):
    marker = "完整参数" * 220
    captured = {}

    def fake_call_service(url, method="GET", params=None, timeout=180, as_form=False):
        captured.update({"url": url, "method": method, "params": params, "as_form": as_form})
        return {"ok": True, "data": [{"data": []}]}

    monkeypatch.setattr("microharness.services.http_client.call_service", fake_call_service)
    svc = {
        "id": "lab-results",
        "name": "lab-results",
        "label": "检验指标查询",
        "url": "http://127.0.0.1:9091/test",
        "method": "POST",
        "request_wrapper": "params",
        "request_map": {"data": {"condition": "{{condition}}", "hdcEncId": "{{global_visit_id}}"}},
    }

    call_service_as_binding(
        svc,
        {"condition": marker},
        global_visit_id="00001_174",
    )

    output = capsys.readouterr().out
    assert "[外部API][完整入参] lab-results POST" in output
    assert marker in output
    assert '"hdcEncId": "00001_174"' in output
    assert '"page": 1' in output
    assert '"rows": 200' in output
    assert "Content-Type=application/x-www-form-urlencoded" in output
    assert "FormBody=params=" in output
    assert captured["params"]["page"] == 1
    assert captured["params"]["rows"] == 200
    assert captured["as_form"] is True


def test_patient_request_uses_project_identifiers_in_wrapped_form(monkeypatch, capsys):
    captured = {}

    def fake_call_service(url, method="GET", params=None, timeout=180, as_form=False):
        captured.update({"params": params, "as_form": as_form})
        return {"ok": True, "data": [{"data": []}]}

    monkeypatch.setattr("microharness.services.http_client.call_service", fake_call_service)
    svc = {
        "id": "drug-interaction",
        "label": "用药医嘱查询",
        "url": "http://10.30.2.139:9091/emviewdoctor/hdc/SerachQuery/MES0005",
        "method": "POST",
        "request_wrapper": "params",
        "request_map": {
            "data": {
                "businessFieldCode": "{{global_visit_id._bfc}}",
                "hdcPatientId": "{{global_patient_id}}",
                "hdcEncId": "{{global_visit_id}}",
            }
        },
    }

    call_service_as_binding(
        svc,
        {"condition": "术前48小时使用过泮托拉唑钠肠溶片"},
        register_no="0004925901",
        visit_no="18759371",
        global_patient_id="00001_4927021",
        global_visit_id="00001_18759371",
    )

    wrapped = json.loads(captured["params"]["params"])
    assert wrapped == {
        "data": {
            "businessFieldCode": "00001",
            "hdcPatientId": "00001_4927021",
            "hdcEncId": "00001_18759371",
        }
    }
    assert captured["params"]["page"] == 1
    assert captured["params"]["rows"] == 200
    assert captured["as_form"] is True
    assert '"hdcPatientId": "00001_4927021"' in capsys.readouterr().out


def test_patient_request_derives_global_visit_id_from_visit_no(monkeypatch):
    captured = {}

    def fake_call_service(url, method="GET", params=None, timeout=180, as_form=False):
        captured.update({"params": params, "as_form": as_form})
        return {"ok": True, "data": [{"data": []}]}

    monkeypatch.setattr("microharness.services.http_client.call_service", fake_call_service)
    svc = {
        "id": "encounter-info",
        "label": "encounter-info",
        "url": "http://127.0.0.1:9091/emviewdoctor/hdc/SerachQuery/MES0002",
        "method": "POST",
        "request_wrapper": "params",
        "request_map": {
            "data": {
                "businessFieldCode": "{{global_visit_id._bfc}}",
                "hdcPatientId": "{{global_patient_id}}",
                "hdcEncId": "{{global_visit_id}}",
            }
        },
    }

    call_service_as_binding(
        svc,
        {"condition": "duration"},
        register_no="0000000120",
        visit_no="174",
        global_patient_id="00001_120",
        global_visit_id="",
    )

    wrapped = json.loads(captured["params"]["params"])
    assert wrapped == {
        "data": {
            "businessFieldCode": "00001",
            "hdcPatientId": "00001_120",
            "hdcEncId": "00001_174",
        }
    }
    assert captured["as_form"] is True


def test_internal_service_contract_is_not_submitted_to_external_api(monkeypatch):
    captured = {}

    def fake_call_service(url, method="GET", params=None, timeout=180, as_form=False):
        captured.update({"params": params, "as_form": as_form})
        return {"ok": True, "data": [{"data": []}]}

    monkeypatch.setattr("microharness.services.http_client.call_service", fake_call_service)
    svc = {
        "id": "future-genomics",
        "label": "Future genomics",
        "url": "http://127.0.0.1:9091/future",
        "method": "POST",
        "request_wrapper": "params",
        "request_map": {"data": {"encounter": "{{global_visit_id}}"}},
        "_contract": {
            "valid": True,
            "level": "complete",
            "internal_marker": "must-not-leak",
        },
    }

    call_service_as_binding(
        svc,
        {"condition": "future condition"},
        global_visit_id="future-encounter-1",
    )

    wrapped = json.loads(captured["params"]["params"])
    assert wrapped == {"data": {"encounter": "future-encounter-1"}}
    assert "_contract" not in captured["params"]
    assert "must-not-leak" not in captured["params"]["params"]
    assert captured["as_form"] is True


def test_missing_request_map_is_blocked_as_configuration_error(monkeypatch, capsys):
    called = False

    def fake_call_service(*args, **kwargs):
        nonlocal called
        called = True
        return {"ok": True, "data": []}

    monkeypatch.setattr("microharness.services.http_client.call_service", fake_call_service)

    result = call_service_as_binding(
        {
            "id": "lab-results",
            "label": "检验指标查询",
            "url": "http://127.0.0.1:9091/test",
            "request_wrapper": "params",
        },
        {"condition": "白细胞>1.5x10^9/L"},
        global_patient_id="00001_4927021",
        global_visit_id="00001_18759371",
    )

    assert called is False
    assert result[0]["service_error"] is True
    assert result[0]["debug_error"] == "service request_map is empty"
    output = capsys.readouterr().out
    assert "[外部API][配置异常] lab-results request_map为空" in output
    assert "已阻止无患者范围的外部接口调用" in output
