from microharness.medical.evidence_plan import (
    apply_evidence_plan_to_analysis,
    build_evidence_plan,
)
from microharness.medical.query_ir import build_query_ir


DOCUMENTS = {
    "\u5165\u9662\u8bb0\u5f55": {
        "purpose": "admission evidence",
        "aliases": ["admission-note"],
        "sections": [
            {"name": "\u521d\u6b65\u8bca\u65ad", "aliases": ["admission-diagnosis"]},
            {"name": "\u73b0\u75c5\u53f2"},
        ],
    },
    "\u51fa\u9662\u8bb0\u5f55": {
        "purpose": "discharge evidence",
        "sections": [{"name": "\u51fa\u9662\u8bca\u65ad"}],
    },
}

SERVICES = {
    "base_url": "http://example.invalid",
    "diagnosis-query": {"aliases": ["diagnosis"], "url": "/diagnosis"},
}


def test_evidence_plan_preserves_multiple_source_types():
    text = "\u70e7\u4f24"
    query_ir = build_query_ir(
        {
            "conditions": [
                {
                    "text": text,
                    "domain": "diagnosis",
                    "target_skills": ["diagnosis-query"],
                    "target_docs": ["\u5165\u9662\u8bb0\u5f55", "\u51fa\u9662\u8bb0\u5f55"],
                    "target_sections": ["\u521d\u6b65\u8bca\u65ad", "\u51fa\u9662\u8bca\u65ad"],
                }
            ]
        },
        text,
    )
    plan = build_evidence_plan(query_ir, DOCUMENTS, SERVICES)
    sources = plan.conditions[0].sources

    assert {source.source_type for source in sources} == {"service", "document"}
    assert {source.resolved_name for source in sources} == {
        "diagnosis-query",
        "\u5165\u9662\u8bb0\u5f55",
        "\u51fa\u9662\u8bb0\u5f55",
    }


def test_evidence_plan_resolves_metadata_aliases():
    query_ir = build_query_ir(
        {
            "conditions": [
                {
                    "text": "x",
                    "target_skills": ["diagnosis"],
                    "target_docs": ["admission-note"],
                    "target_sections": ["admission-diagnosis"],
                }
            ]
        },
        "x",
    )
    source_plan = build_evidence_plan(query_ir, DOCUMENTS, SERVICES).conditions[0].sources

    assert source_plan[0].resolved_name == "diagnosis-query"
    assert source_plan[1].resolved_name == "\u5165\u9662\u8bb0\u5f55"
    assert source_plan[1].sections == ["\u521d\u6b65\u8bca\u65ad"]


def test_unknown_document_is_retained_for_diagnostics():
    query_ir = build_query_ir(
        {
            "conditions": [
                {
                    "text": "x",
                    "target_docs": ["unknown-note"],
                    "target_sections": ["unknown-section"],
                }
            ]
        },
        "x",
    )
    plan = build_evidence_plan(query_ir, DOCUMENTS, SERVICES)
    condition = plan.conditions[0]

    assert plan.unresolved_count == 1
    assert condition.sources[0].requested_name == "unknown-note"
    assert condition.sources[0].resolution_status == "unresolved"
    assert condition.diagnostics[0]["code"] == "UNRESOLVED_DOCUMENT"


def test_missing_route_candidates_are_explicit():
    query_ir = build_query_ir({"conditions": [{"text": "x"}]}, "x")
    condition = build_evidence_plan(query_ir, DOCUMENTS, SERVICES).conditions[0]

    assert not condition.sources
    assert condition.diagnostics == [
        {
            "code": "NO_EVIDENCE_SOURCE_PLANNED",
            "condition_id": "c1",
            "domain": "clinical_concept",
        }
    ]


def test_metadata_roles_supplement_missing_router_candidates():
    documents = {
        "admission-note": {
            "sections": [
                {"name": "diagnosis", "evidence_roles": ["diagnosis_evidence"]},
                {"name": "allergy", "evidence_roles": ["allergy_evidence"]},
            ]
        }
    }
    services = {
        "diagnosis-api": {
            "url": "/diagnosis",
            "semantic": {
                "domain": "diagnosis",
                "evidence_types": ["diagnosis_evidence"],
            },
        }
    }
    query_ir = build_query_ir(
        {"conditions": [{"text": "开放疾病概念", "domain": "diagnosis"}]},
        "开放疾病概念",
    )

    sources = build_evidence_plan(query_ir, documents, services).conditions[0].sources

    assert {(source.source_type, source.resolved_name) for source in sources} == {
        ("service", "diagnosis-api"),
        ("document", "admission-note"),
    }
    document_source = next(source for source in sources if source.source_type == "document")
    assert document_source.sections == ["diagnosis"]
    assert all(source.reason == "metadata_role_match" for source in sources)


def test_evidence_plan_adapter_injects_only_resolved_sources():
    analysis = {
        "conditions": [
            {
                "text": "开放疾病概念",
                "domain": "diagnosis",
                "target_skills": ["unknown-api"],
                "target_docs": ["unknown-note"],
                "target_sections": [],
            }
        ]
    }
    query_ir = build_query_ir(analysis, "开放疾病概念")
    documents = {
        "admission-note": {
            "sections": [
                {"name": "diagnosis", "evidence_roles": ["diagnosis_evidence"]}
            ]
        }
    }
    services = {
        "diagnosis-api": {
            "url": "/diagnosis",
            "semantic": {"evidence_types": ["diagnosis_evidence"]},
        }
    }
    plan = build_evidence_plan(query_ir, documents, services)

    enriched = apply_evidence_plan_to_analysis(analysis, plan)
    condition = enriched["conditions"][0]

    assert condition["target_skills"] == ["unknown-api", "diagnosis-api"]
    assert condition["target_docs"] == ["unknown-note", "admission-note"]
    assert condition["target_sections"] == ["diagnosis"]
    assert condition["targets"] == {"admission-note": ["diagnosis"]}
    assert all("unknown" not in source_id for source_id in condition["evidence_plan_source_ids"])
    assert enriched["evidence_plan_version"] == plan.version
