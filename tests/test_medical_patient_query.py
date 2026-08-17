import pytest

from microharness.medical.patient_query import (
    MissingPatientIdentityError,
    build_patient_where_clause,
)
from web.app import (
    _extract_core_keyword,
    _precompute_hints,
    _primary_service_for_condition,
    _resolve_medical_query_visit_context,
    _resolve_executable_route_sources,
    _source_display_label,
)


class _FakeClient:
    def __init__(self):
        self.sql = []

    def execute(self, sql):
        self.sql.append(sql)
        return []


class _FakeDatabase:
    def __init__(self):
        self.config = {"type": "test"}
        self.client = _FakeClient()
        self.test_calls = 0

    def test(self):
        self.test_calls += 1
        return True


def test_source_display_label_uses_dynamic_catalog_and_keeps_unknown_source_id():
    catalog = {
        "future-source": {
            "label": "未来来源查询",
            "name": "未来来源内部名称",
        }
    }

    assert _source_display_label("future-source.event_time", catalog) == "未来来源查询"
    assert _source_display_label("unregistered-source.event_time", catalog) == "unregistered-source"


@pytest.mark.parametrize("condition", ["发烧的患者", "患者发烧", "患者有发烧"])
def test_extract_core_keyword_removes_patient_query_subject(condition):
    assert _extract_core_keyword(condition) == "发烧"


@pytest.mark.parametrize(
    "fields_text",
    [
        "出院日期时间: 2024-09-15 08:36:00\n入院日期时间: 2024-09-12 09:12:00",
        "入院日期时间: 2024-09-12 09:12:00\n出院日期时间: 2024-09-15 08:36:00",
    ],
)
def test_precompute_date_difference_uses_later_minus_earlier_field(fields_text):
    hints = _precompute_hints(fields_text)

    assert "出院日期时间 - 入院日期时间(天) = 2天" in hints
    assert "入院日期时间 - 出院日期时间" not in hints


def test_patient_where_requires_at_least_one_identity():
    with pytest.raises(MissingPatientIdentityError):
        build_patient_where_clause()


def test_global_patient_identity_never_generates_unscoped_fallback():
    clause = build_patient_where_clause(global_patient_id="00001_120")

    assert clause.strict_where == "papat_relpatientid = '00001_120'"
    assert clause.fallback_where is None
    assert "1=1" not in clause.strict_where


def test_local_identity_is_the_only_compatibility_fallback():
    clause = build_patient_where_clause(
        register_no="0000000120",
        visit_no="174",
        global_patient_id="00001_120",
        global_visit_id="00001_174",
    )

    assert "registerno = '0000000120'" in clause.strict_where
    assert "visitnumber = '174'" in clause.strict_where
    assert "papat_relpatientid = '00001_120'" in clause.strict_where
    assert "paadm_relvisitnumber = '00001_174'" in clause.strict_where
    assert clause.fallback_where == "registerno = '0000000120' AND visitnumber = '174'"
    assert "1=1" not in clause.strict_where
    assert "1=1" not in clause.fallback_where


def test_patient_identity_values_are_quoted_without_changing_scope():
    clause = build_patient_where_clause(register_no="R'120\n")

    assert clause.strict_where == "registerno = 'R''120 '"
    assert clause.fallback_where is None


def test_query_visit_context_uses_single_local_visit(tmp_path, monkeypatch):
    import json
    import web.app as web_app

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
    monkeypatch.setattr(web_app, "_PATIENTS_DIR", tmp_path)

    assert _resolve_medical_query_visit_context("0000000120", "", "", "") == (
        "174",
        "00001_120",
        "00001_174",
    )


def test_query_visit_context_fills_requested_visit_metadata(tmp_path, monkeypatch):
    import json
    import web.app as web_app

    visit_dir = tmp_path / "0000000120" / "174"
    visit_dir.mkdir(parents=True)
    (visit_dir / "_visit.json").write_text(
        json.dumps({"visit_no": "174", "global_visit_id": "00001_174"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(web_app, "_PATIENTS_DIR", tmp_path)

    assert _resolve_medical_query_visit_context(
        "0000000120", "174", "00001_120", ""
    ) == ("174", "00001_120", "00001_174")


def test_query_visit_context_does_not_guess_among_multiple_visits(tmp_path, monkeypatch):
    import json
    import web.app as web_app

    patient_dir = tmp_path / "0000000120"
    (patient_dir / "174").mkdir(parents=True)
    (patient_dir / "175").mkdir(parents=True)
    (patient_dir / "_meta.json").write_text(
        json.dumps({"global_patient_id": "00001_120"}), encoding="utf-8"
    )
    monkeypatch.setattr(web_app, "_PATIENTS_DIR", tmp_path)

    assert _resolve_medical_query_visit_context("0000000120", "", "", "") == (
        "",
        "00001_120",
        "",
    )


def test_query_db_with_only_global_identity_runs_one_scoped_query(monkeypatch):
    from microharness.database import db_client
    from web.app import _query_db

    fake_db = _FakeDatabase()
    monkeypatch.setattr(db_client, "get_db", lambda: fake_db)

    result = _query_db(
        {"targets": {"入院记录": ["主诉"]}},
        register_no="",
        visit_no="",
        global_patient_id="00001_120",
        global_visit_id="",
    )

    assert result == []
    assert len(fake_db.client.sql) == 1
    assert "papat_relpatientid = '00001_120'" in fake_db.client.sql[0]
    assert "WHERE 1=1" not in fake_db.client.sql[0]


def test_query_db_fallback_keeps_local_patient_scope(monkeypatch):
    from microharness.database import db_client
    from web.app import _query_db

    fake_db = _FakeDatabase()
    monkeypatch.setattr(db_client, "get_db", lambda: fake_db)

    result = _query_db(
        {"targets": {"入院记录": ["主诉"]}},
        register_no="0000000120",
        visit_no="174",
        global_patient_id="00001_120",
        global_visit_id="00001_174",
    )

    assert result == []
    assert len(fake_db.client.sql) == 2
    assert "papat_relpatientid = '00001_120'" in fake_db.client.sql[0]
    fallback_where = fake_db.client.sql[1].split(" WHERE ", 1)[1]
    assert "registerno = '0000000120'" in fallback_where
    assert "visitnumber = '174'" in fallback_where
    assert "papat_relpatientid" not in fallback_where
    assert all("WHERE 1=1" not in sql for sql in fake_db.client.sql)


def test_query_db_without_identity_stops_before_database_access(monkeypatch):
    from microharness.database import db_client
    from web.app import _query_db

    monkeypatch.setattr(
        db_client,
        "get_db",
        lambda: (_ for _ in ()).throw(AssertionError("database should not be accessed")),
    )

    result = _query_db(
        {"targets": {"入院记录": ["主诉"]}},
        register_no="",
        visit_no="",
        global_patient_id="",
        global_visit_id="",
    )

    assert result[0]["error_code"] == "MISSING_PATIENT_IDENTITY"
    assert result[0]["service_error"] is True


def test_query_db_uses_request_scoped_health_check(monkeypatch):
    from microharness.database import db_client
    from web.app import _query_db

    fake_db = _FakeDatabase()
    health_calls = []
    monkeypatch.setattr(db_client, "get_db", lambda: fake_db)

    result = _query_db(
        {"targets": {"入院记录": ["主诉"]}},
        register_no="0000000120",
        visit_no="174",
        global_patient_id="00001_120",
        global_visit_id="00001_174",
        db_health_check=lambda db: (health_calls.append(db) is None, ""),
    )

    assert result == []
    assert health_calls == [fake_db]
    assert fake_db.test_calls == 0


@pytest.mark.parametrize(
    ("target_docs", "service_candidates", "expected_docs", "expected_services", "should_fallback"),
    [
        (["入院记录"], [{"id": "lab-results"}], ["入院记录"], ["lab-results"], False),
        (["入院记录"], [], ["入院记录"], [], False),
        ([], [{"id": "lab-results"}], [], ["lab-results"], False),
        ([], [], [], [], True),
        (["未知文档"], [], [], [], True),
    ],
)
def test_executable_route_source_matrix(
    target_docs,
    service_candidates,
    expected_docs,
    expected_services,
    should_fallback,
):
    result = _resolve_executable_route_sources(
        target_docs=target_docs,
        service_candidates=service_candidates,
        document_catalog={"入院记录": {"purpose": "入院信息"}},
        service_catalog={"lab-results": {"url": "SerachQuery/MES0023"}},
        table_map={"入院记录": {"table": "MR_ADMISSION"}},
    )

    assert result["documents"] == expected_docs
    assert result["services"] == expected_services
    assert result["should_fallback"] is should_fallback


def test_route_source_resolution_reports_unexecutable_candidates():
    result = _resolve_executable_route_sources(
        target_docs=["未知文档", "无表映射文档"],
        service_candidates=[{"id": "missing-service"}, {"id": "no-url-service"}],
        document_catalog={"无表映射文档": {"purpose": "仅外部元数据存在"}},
        service_catalog={"no-url-service": {"url": ""}},
        table_map={},
    )

    assert result["unresolved_documents"] == ["未知文档", "无表映射文档"]
    assert result["unresolved_services"] == ["missing-service", "no-url-service"]
    assert result["should_fallback"] is True


def test_modern_ir_primary_service_does_not_scan_text_triggers(monkeypatch):
    from microharness.services import service_catalog

    monkeypatch.setattr(
        service_catalog,
        "load_services",
        lambda: (_ for _ in ()).throw(
            AssertionError("modern IR must not load trigger metadata")
        ),
    )

    assert _primary_service_for_condition(
        "患者使用过某药", {}, allow_text_fallback=False
    ) == ""


def test_legacy_primary_service_can_use_configured_text_triggers(monkeypatch):
    from microharness.services import service_catalog

    monkeypatch.setattr(
        service_catalog,
        "load_services",
        lambda: {"drug-interaction": {"triggers": ["使用过"]}},
    )

    assert _primary_service_for_condition(
        "患者使用过某药", {}, allow_text_fallback=True
    ) == "drug-interaction"


@pytest.mark.parametrize(
    ("condition", "semantic", "expected"),
    [
        ("开放检验条件", {"target_skills": ["lab-results"]}, "lab-results"),
        ("开放用药条件", {"entity_type": "drug"}, "drug-interaction"),
        ("开放诊断条件", {"semantic_class": "疾病/症状存在"}, "diagnosis-query"),
    ],
)
def test_explicit_ir_primary_service_selection_is_unchanged(condition, semantic, expected):
    assert _primary_service_for_condition(
        condition, semantic, allow_text_fallback=False
    ) == expected
