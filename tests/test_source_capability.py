from microharness.medical.source_capability import (
    ADMINISTRATION_EVENT,
    ENTITY_PRESENCE,
    NUMERIC_VALUE,
    ORDER_EVENT,
    STATUS_VALIDITY,
    TEMPORAL_OCCURRENCE,
    build_source_capability_profile,
    semantic_capability_enabled,
)


def test_semantic_evidence_capabilities_extend_supported_without_skill_id():
    profile = build_source_capability_profile(
        domain="future-source",
        semantic_type="ENTITY_PRESENCE",
        required_capabilities=[ENTITY_PRESENCE, TEMPORAL_OCCURRENCE],
        semantic={
            "evidence_capabilities": {
                "finding": True,
                "performed_at": True,
            }
        },
    )

    assert TEMPORAL_OCCURRENCE in profile.supported_capabilities
    assert profile.missing_capabilities == ()


def test_missing_is_derived_from_declared_requirement_and_supported_profile():
    profile = build_source_capability_profile(
        domain="future-source",
        semantic_type="NUMERIC_COMPARISON",
        required_capabilities=[ENTITY_PRESENCE],
        raw={"required_capabilities": [NUMERIC_VALUE]},
    )

    assert NUMERIC_VALUE in profile.required_capabilities
    assert NUMERIC_VALUE in profile.missing_capabilities


def test_raw_nested_capability_profile_can_support_future_sources():
    profile = build_source_capability_profile(
        domain="future-source",
        semantic_type="NUMERIC_COMPARISON",
        required_capabilities=[ENTITY_PRESENCE],
        raw={
            "capability_profile": {
                "supported_capabilities": [NUMERIC_VALUE],
                "required_capabilities": [NUMERIC_VALUE],
            }
        },
    )

    assert NUMERIC_VALUE in profile.supported_capabilities
    assert profile.missing_capabilities == ()


def test_medication_administration_missing_uses_predicate_not_skill_id():
    profile = build_source_capability_profile(
        domain="medication",
        semantic_type="MEDICATION_ADMINISTRATION",
        required_capabilities=[ENTITY_PRESENCE, ADMINISTRATION_EVENT],
        semantic={"evidence_capabilities": {"ordered": True, "administered": False}},
        predicate="administered",
    )

    assert ORDER_EVENT in profile.supported_capabilities
    assert ADMINISTRATION_EVENT in profile.required_capabilities
    assert ADMINISTRATION_EVENT in profile.missing_capabilities


def test_predicate_policy_declares_status_contract_generically():
    profile = build_source_capability_profile(
        domain="medication",
        semantic_type="MEDICATION_ORDER",
        required_capabilities=[ENTITY_PRESENCE, ORDER_EVENT],
        predicate="ordered",
        policy={"required_status": True},
    )

    assert STATUS_VALIDITY in profile.supported_capabilities
    assert STATUS_VALIDITY in profile.required_capabilities
    assert profile.missing_capabilities == ()


def test_semantic_capability_enabled_accepts_aliases():
    semantic = {"evidence_capabilities": {"performed_at": True, "administered": False}}

    assert semantic_capability_enabled(semantic, TEMPORAL_OCCURRENCE) is True
    assert semantic_capability_enabled(semantic, ADMINISTRATION_EVENT) is False
