from microharness.medical.record_identity import (
    display_record_reference,
    identity_from_binding,
    record_identity_config,
    resolve_record_identity,
)


SEMANTIC = {
    "presentation": {
        "record_identity": {
            "label": "业务号",
            "fields": ["primaryId", "fallbackId"],
        }
    }
}


def test_record_identity_uses_declared_field_priority():
    identity = resolve_record_identity(
        {"fallbackId": "SECOND", "primaryId": "FIRST"},
        SEMANTIC,
    )

    assert identity == {
        "record_id": "FIRST",
        "record_id_label": "业务号",
        "record_id_field": "primaryId",
    }


def test_record_identity_falls_back_from_empty_primary_value():
    identity = resolve_record_identity(
        {"primaryId": "  ", "fallbackId": "SECOND"},
        SEMANTIC,
    )

    assert identity["record_id"] == "SECOND"
    assert identity["record_id_field"] == "fallbackId"


def test_record_identity_matches_api_field_names_case_insensitively():
    identity = resolve_record_identity({"PRIMARYID": "FIRST"}, SEMANTIC)

    assert identity["record_id"] == "FIRST"
    assert identity["record_id_field"] == "PRIMARYID"


def test_record_identity_keeps_numeric_zero_identifier():
    identity = resolve_record_identity({"primaryId": 0}, SEMANTIC)

    assert identity["record_id"] == "0"


def test_record_identity_without_metadata_is_empty_and_uses_internal_fallback():
    assert record_identity_config({}) == {"label": "", "fields": []}
    assert resolve_record_identity({"primaryId": "FIRST"}, {}) == {}
    assert identity_from_binding({"record_id": ""}) == {}
    assert display_record_reference("[记录7]", "", "业务号") == "[记录7]"


def test_display_record_reference_prefers_business_identifier():
    assert display_record_reference("[记录7]", "FIRST", "业务号") == "业务号=FIRST"
