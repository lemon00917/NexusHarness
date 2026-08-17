from microharness.services.service_catalog import (
    _merge_config_services,
    load_services,
    validate_service_catalog,
    validate_service_contract,
)


def _skill_services():
    return {
        "base_url": "http://127.0.0.1:9091/base/",
        "lab-results": {
            "name": "检验指标查询",
            "label": "检验指标查询",
            "url": "SerachQuery/MES0023",
            "method": "POST",
            "request_wrapper": "params",
            "request_map": {
                "data": {
                    "businessFieldCode": "{{global_visit_id._bfc}}",
                    "hdcPatientId": "{{global_patient_id}}",
                    "hdcEncId": "{{global_visit_id}}",
                }
            },
            "keep_fields": ["inspItemDesc", "inspectionValue"],
            "semantic": {"domain": "laboratory"},
        },
    }


def test_partial_runtime_config_keeps_skill_request_metadata():
    services = _skill_services()
    cfg = {
        "services": {
            "lab-results": {
                "name": "检验服务",
                "url": "http://10.30.2.139:9091/emviewdoctor/hdc/SerachQuery/MES0023",
                "triggers": ["检验", "白细胞"],
            }
        }
    }

    merged = _merge_config_services(services, cfg)["lab-results"]

    assert merged["url"].startswith("http://10.30.2.139:9091/")
    assert merged["name"] == "检验服务"
    assert merged["request_wrapper"] == "params"
    assert merged["request_map"]["data"]["hdcEncId"] == "{{global_visit_id}}"
    assert merged["keep_fields"] == ["inspItemDesc", "inspectionValue"]
    assert merged["semantic"] == {"domain": "laboratory"}


def test_empty_structural_overrides_do_not_erase_skill_metadata():
    services = _skill_services()
    cfg = {
        "services": {
            "lab-results": {
                "url": "SerachQuery/MES0023",
                "request_wrapper": "",
                "request_map": {},
                "keep_fields": [],
                "semantic": {},
            }
        }
    }

    merged = _merge_config_services(services, cfg)["lab-results"]

    assert merged["request_wrapper"] == "params"
    assert merged["request_map"]["data"]["hdcPatientId"] == "{{global_patient_id}}"
    assert merged["keep_fields"] == ["inspItemDesc", "inspectionValue"]
    assert merged["semantic"] == {"domain": "laboratory"}


def test_new_custom_service_can_still_be_loaded_from_config():
    services = {"base_url": "http://127.0.0.1:9091/base/"}
    custom = {
        "url": "SerachQuery/CUSTOM",
        "request_map": {"data": {"hdcEncId": "{{global_visit_id}}"}},
    }

    merged = _merge_config_services(services, {"services": {"custom": custom}})

    assert merged["custom"] == custom


def _future_service():
    return {
        "name": "Future imaging query",
        "label": "Future imaging",
        "url": "Search/FUTURE001",
        "method": "POST",
        "triggers": ["future image"],
        "request_map": {"data": {"encounter": "{{global_visit_id}}"}},
        "semantic": {
            "entity_type": "imaging",
            "domain": "imaging",
            "evidence_types": ["imaging_evidence"],
            "temporal_filter_mode": "domain",
            "presentation": {"record_type": "imaging_report"},
            "evidence_capabilities": {"finding": True, "performed_at": True},
        },
    }


def test_future_skill_contract_is_complete_without_known_service_ids():
    report = validate_service_contract(
        "future-imaging-v2",
        _future_service(),
        require_semantic=True,
    )

    assert report["valid"] is True
    assert report["level"] == "complete"
    assert report["errors"] == []
    assert report["normalized"] == {
        "source_kind": "service",
        "temporal_filter_mode": "domain",
        "record_type": "imaging_report",
        "entity_type": "imaging",
        "domain": "imaging",
        "evidence_types": ["imaging_evidence"],
    }


def test_skill_contract_rejects_malformed_generic_metadata():
    service = _future_service()
    service["semantic"] = {
        "entity_type": "risk",
        "domain": "risk",
        "evidence_types": "risk_evidence",
        "temporal_filter_mode": "hospital-specific",
        "presentation": [],
        "evidence_capabilities": ["score"],
    }

    report = validate_service_contract(
        "future-risk-score",
        service,
        require_semantic=True,
    )
    codes = {item["code"] for item in report["errors"]}

    assert report["valid"] is False
    assert report["level"] == "invalid"
    assert codes == {
        "INVALID_EVIDENCE_TYPES",
        "INVALID_TEMPORAL_FILTER_MODE",
        "INVALID_PRESENTATION",
        "INVALID_EVIDENCE_CAPABILITIES",
    }


def test_skill_backed_service_requires_semantic_contract():
    service = _future_service()
    service.pop("semantic")

    report = validate_service_contract(
        "future-pathology",
        service,
        require_semantic=True,
    )

    assert report["valid"] is False
    assert "MISSING_SEMANTIC_CONTRACT" in {
        item["code"] for item in report["errors"]
    }


def test_config_only_legacy_service_remains_compatible():
    service = {
        "url": "Search/LEGACY",
        "request_map": {"patient": "{{global_patient_id}}"},
    }

    report = validate_service_contract(
        "legacy-custom",
        service,
        require_semantic=False,
    )

    assert report["valid"] is True
    assert report["level"] == "compatible"
    assert report["errors"] == []
    assert "MISSING_SEMANTIC_CONTRACT" in {
        item["code"] for item in report["warnings"]
    }


def test_catalog_report_separates_complete_compatible_and_invalid_services():
    report = validate_service_catalog(
        {
            "base_url": "http://127.0.0.1/",
            "future-complete": _future_service(),
            "legacy-compatible": {"url": "Search/LEGACY"},
            "Invalid Name": _future_service(),
        },
        strict_service_ids={"future-complete", "Invalid Name"},
    )

    assert report["valid"] is False
    assert report["counts"] == {"complete": 1, "compatible": 1, "invalid": 1}


def test_load_services_attaches_contract_to_future_skill(monkeypatch, tmp_path):
    from microharness.services import service_catalog

    skills_dir = tmp_path / "skills"
    skill_dir = skills_dir / "future-genomics"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: future-genomics
description: Future genomics query
metadata:
  semantic:
    entity_type: genomics
    domain: genomics
    evidence_types: [genomics_evidence]
    temporal_filter_mode: generic
  triggers: [gene]
  api:
    url: Search/FUTUREGENE
    method: POST
    request_map:
      data:
        encounter: "{{global_visit_id}}"
---
# Future genomics
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(service_catalog, "_SKILLS_DIR", skills_dir)
    monkeypatch.setattr(service_catalog, "_CONFIG_PATH", tmp_path / "missing.json")

    services = load_services()

    assert services["future-genomics"]["_contract"]["level"] == "complete"
    assert services["future-genomics"]["_contract"]["valid"] is True
