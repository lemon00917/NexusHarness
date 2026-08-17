from datetime import datetime

from microharness.medical.diagnosis_rules import judge_diagnosis_condition
from microharness.medical.evidence import annotate_evidence_source
from microharness.medical.lab_rules import judge_lab_condition
from microharness.medical.medication_rules import judge_medication_condition
from microharness.medical.structured_time import filter_bindings_by_time_window
from microharness.medical.time_window import TimeWindow
from microharness.services.service_catalog import load_services, validate_service_contract


LAB_REPORT_LABEL = "\u68c0\u9a8c\u62a5\u544a\u53f7"
ORDER_LABEL = "\u533b\u5631\u53f7"
DIAGNOSIS_LABEL = "\u8bca\u65adID"
ENCOUNTER_LABEL = "\u5c31\u8bca\u53f7"


def _identity(record_id, label, field):
    return {
        "record_id": record_id,
        "record_id_label": label,
        "record_id_field": field,
    }


def test_lab_reason_and_candidate_use_report_number():
    identity = _identity("RPT-77", LAB_REPORT_LABEL, "inspRptId")
    bindings = [
        {"html_field": "[\u68c0\u9a8c34] \u5316\u9a8c\u9879\u76ee\u63cf\u8ff0", "eng_field": "inspItemDesc", "value": "\u4e2d\u6027\u7c92\u7ec6\u80de\u6570", **identity},
        {"html_field": "[\u68c0\u9a8c34] \u7ed3\u679c", "eng_field": "inspectionValue", "value": "4.00", **identity},
        {"html_field": "[\u68c0\u9a8c34] \u5355\u4f4d", "eng_field": "inspResultUnitCode", "value": "*10^9/L", **identity},
    ]

    result = judge_lab_condition(
        "\u4e2d\u6027\u7c92\u7ec6\u80de\u6570>1.5x10^9/L",
        bindings,
        None,
    )

    candidate = result["candidate_records"][0]
    assert candidate["\u8bb0\u5f55"] == f"{LAB_REPORT_LABEL}=RPT-77"
    assert candidate["\u8bb0\u5f55ID"] == "RPT-77"
    assert candidate["\u8bb0\u5f55\u6807\u8bc6\u5b57\u6bb5"] == "inspRptId"
    assert "[\u68c0\u9a8c34]" not in result["reason"]


def test_medication_reason_uses_order_number_not_prescription_number():
    identity = _identity("ORD-594", ORDER_LABEL, "hosOrdId")
    semantic = {
        "domain": "medication",
        "entity_type": "drug",
        "predicate": "ordered",
        "evidence_capabilities": {"ordered": True, "status": True},
        "fields": {
            "entity": ["orderName"],
            "record_id": ["hosOrdId", "hdcOrdId"],
            "ordered_at": ["orderedAt"],
            "status": ["ordStatusDesc"],
        },
        "predicate_policies": {
            "ordered": {
                "time_basis": "ordered_at",
                "require_status": True,
                "accepted_status_terms": ["\u6709\u6548"],
                "invalid_status_terms": ["\u64a4\u9500"],
            }
        },
    }
    bindings = [
        {"html_field": "[\u7528\u836f17] \u836f\u7269\u540d\u79f0", "eng_field": "orderName", "value": "\u963f\u53f8\u5339\u6797\u80a0\u6eb6\u7247", **identity},
        {"html_field": "[\u7528\u836f17] \u5904\u65b9\u53f7", "eng_field": "medPrescNo", "value": "RX-48", **identity},
        {"html_field": "[\u7528\u836f17] \u5f00\u7acb\u65e5\u671f\u65f6\u95f4", "eng_field": "orderedAt", "value": "2026-06-10 10:00:00", **identity},
        {"html_field": "[\u7528\u836f17] \u533b\u5631\u72b6\u6001\u63cf\u8ff0", "eng_field": "ordStatusDesc", "value": "\u6709\u6548", **identity},
    ]

    result = judge_medication_condition(
        "\u5f00\u8fc7\u963f\u53f8\u5339\u6797",
        bindings,
        entity="\u963f\u53f8\u5339\u6797",
        semantic=semantic,
    )

    candidate = result["candidate_records"][0]
    assert candidate["\u8bb0\u5f55"] == f"{ORDER_LABEL}=ORD-594"
    assert candidate["\u8bb0\u5f55ID"] == "ORD-594"
    assert candidate["\u5904\u65b9\u53f7"] == "RX-48"
    assert "[\u7528\u836f17]" not in result["reason"]


def test_diagnosis_reason_and_candidate_use_diagnosis_id():
    identity = _identity("DIAG-12", DIAGNOSIS_LABEL, "hosDiagId")
    bindings = [
        {"html_field": "[\u8bca\u65ad2] \u8bca\u65ad\u540d\u79f0", "eng_field": "diagnoseName", "value": "\u80cc\u75db", **identity},
        {"html_field": "[\u8bca\u65ad2] \u8bca\u65ad\u7c7b\u578b", "eng_field": "diagTypeDesc", "value": "\u95e8\u8bca\u8bca\u65ad", **identity},
    ]

    result = judge_diagnosis_condition(
        "\u80cc\u75db",
        bindings,
        entity="\u80cc\u75db",
        semantic={"domain": "diagnosis", "entity_type": "diagnosis"},
    )

    candidate = result["candidate_records"][0]
    assert candidate["\u8bb0\u5f55"] == f"{DIAGNOSIS_LABEL}=DIAG-12"
    assert candidate["\u8bb0\u5f55ID"] == "DIAG-12"
    assert "[\u8bca\u65ad2]" not in result["reason"]


def test_builtin_skills_prefer_hospital_record_identifiers():
    services = load_services()

    assert services["encounter-info"]["semantic"]["presentation"]["record_identity"] == {
        "label": ENCOUNTER_LABEL,
        "fields": ["hosEncId", "hdcEncId"],
    }
    assert services["diagnosis-query"]["semantic"]["presentation"]["record_identity"] == {
        "label": DIAGNOSIS_LABEL,
        "fields": ["hosDiagId", "hdcDiagId"],
    }
    assert services["drug-interaction"]["semantic"]["presentation"]["record_identity"] == {
        "label": ORDER_LABEL,
        "fields": ["hosOrdId", "hdcOrdId"],
    }
    assert services["lab-results"]["semantic"]["presentation"]["record_identity"] == {
        "label": LAB_REPORT_LABEL,
        "fields": ["inspRptId", "hdcInspRptId"],
    }
    assert services["drug-interaction"]["semantic"]["fields"]["record_id"][:2] == [
        "hosOrdId",
        "hdcOrdId",
    ]


def test_generic_time_filter_exposes_encounter_number():
    identity = _identity("174", ENCOUNTER_LABEL, "hosEncId")
    window = TimeWindow(
        scope="\u4f4f\u9662\u671f\u95f4",
        start=datetime(2026, 6, 1),
        end=datetime(2026, 6, 4),
        required=True,
    )
    bindings = [
        {"html_field": "[\u5c31\u8bca1] \u5165\u9662\u65e5\u671f\u65f6\u95f4", "eng_field": "encStartDateTime", "value": "2026-06-02 09:00:00", **identity},
        {"html_field": "[\u5c31\u8bca1] \u5c31\u8bca\u7c7b\u578b", "eng_field": "encTypeDesc", "value": "\u4f4f\u9662", **identity},
    ]

    result = filter_bindings_by_time_window(bindings, window)

    candidate = result["candidate_records"][0]
    assert candidate["\u8bb0\u5f55"] == f"{ENCOUNTER_LABEL}=174"
    assert candidate["\u8bb0\u5f55ID"] == "174"
    assert candidate["\u8bb0\u5f55\u6807\u8bc6\u5b57\u6bb5"] == "hosEncId"


def test_source_annotation_exposes_generic_record_identity_contract():
    file_result = {"file": "Future result"}
    source = {
        "file": "Future result",
        "service_id": "future-skill",
        "semantic": {
            "presentation": {
                "record_type": "future_record",
                "record_identity": {
                    "label": "Future ID",
                    "fields": ["futureId", "fallbackId"],
                },
            }
        },
    }

    annotate_evidence_source(file_result, source)

    assert file_result["record_id_label"] == "Future ID"
    assert file_result["record_id_fields"] == ["futureId", "fallbackId"]


def test_service_contract_validates_generic_record_identity_metadata():
    base = {
        "name": "Future service",
        "label": "Future service",
        "url": "Search/FUTURE",
        "triggers": ["future"],
        "request_map": {"data": {"encounter": "{{global_visit_id}}"}},
        "semantic": {
            "entity_type": "future",
            "domain": "future",
            "evidence_types": ["future_evidence"],
            "temporal_filter_mode": "generic",
            "presentation": {
                "record_type": "future_record",
                "record_identity": {"label": "Future ID", "fields": ["futureId"]},
            },
        },
    }

    valid = validate_service_contract("future-skill", base, require_semantic=True)
    assert valid["valid"] is True

    base["semantic"]["presentation"]["record_identity"] = {"label": "", "fields": []}
    invalid = validate_service_contract("future-skill", base, require_semantic=True)
    assert invalid["valid"] is False
    assert "INVALID_RECORD_IDENTITY" in {item["code"] for item in invalid["errors"]}
