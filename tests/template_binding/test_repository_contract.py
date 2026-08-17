from microharness.template_binding.config import TemplateBindingDatabaseConfig
from microharness.template_binding.repository import (
    TemplateBindingRepository,
    TemplateBindingRepositoryError,
)


def _repository():
    return TemplateBindingRepository(
        TemplateBindingDatabaseConfig(
            host="127.0.0.1",
            port=5432,
            database="dmp",
            schema="sm_dmp",
            user="reader",
            password="",
        )
    )


def test_html_template_detail_requires_composite_key(monkeypatch):
    repository = _repository()
    captured = {}

    def fake_fetch_one(sql, params=()):
        captured["sql"] = sql
        captured["params"] = params
        return None

    monkeypatch.setattr(repository, "_fetch_one", fake_fetch_one)

    repository.get_html_template("template-1", "category-2", include_html=True)

    assert "t.template_id = %s AND t.print_template_category_id = %s" in captured["sql"]
    assert captured["params"] == ("template-1", "category-2")
    assert "t.html_info" in captured["sql"]


def test_html_template_list_does_not_select_html_info(monkeypatch):
    repository = _repository()
    statements = []

    def fake_fetch_one(sql, params=()):
        statements.append(sql)
        return {"total": 0}

    def fake_fetch_all(sql, params=()):
        statements.append(sql)
        return []

    monkeypatch.setattr(repository, "_fetch_one", fake_fetch_one)
    monkeypatch.setattr(repository, "_fetch_all", fake_fetch_all)

    repository.list_html_templates()

    list_sql = statements[-1]
    assert "t.html_info," not in list_sql
    assert "LENGTH(t.html_info)" in list_sql


def test_html_template_variants_are_loaded_by_template_id(monkeypatch):
    repository = _repository()
    captured = {}

    def fake_fetch_all(sql, params=()):
        captured["sql"] = sql
        captured["params"] = params
        return []

    monkeypatch.setattr(repository, "_fetch_all", fake_fetch_all)
    repository.list_html_template_variants("template-1")

    assert "WHERE t.template_id = %s" in captured["sql"]
    assert "t.html_info" in captured["sql"]
    assert captured["params"] == ("template-1",)


def test_repository_error_redacts_password():
    repository = TemplateBindingRepository(
        TemplateBindingDatabaseConfig(
            host="127.0.0.1",
            port=5432,
            database="dmp",
            schema="sm_dmp",
            user="reader",
            password="top-secret",
        )
    )

    message = repository._safe_error(RuntimeError("password=top-secret login failed: top-secret"))

    assert "top-secret" not in message
    assert "password=***" in message


def test_standard_template_and_node_ids_are_returned_as_text(monkeypatch):
    repository = _repository()
    statements = []

    def fake_fetch_one(sql, params=()):
        statements.append(sql)
        return {"total": 0} if "COUNT(*)" in sql else None

    def fake_fetch_all(sql, params=()):
        statements.append(sql)
        return []

    monkeypatch.setattr(repository, "_fetch_one", fake_fetch_one)
    monkeypatch.setattr(repository, "_fetch_all", fake_fetch_all)

    repository.list_standard_templates()
    repository.get_standard_template("2079815869257592833")
    repository.list_standard_nodes("2079815869257592833")

    sql = "\n".join(statements)
    assert "CAST(t.id AS TEXT) AS id" in sql
    assert "t.status = %s" in sql
    assert "CAST(id AS TEXT) AS id" in sql
    assert "CAST(standard_xml_id AS TEXT) AS standard_xml_id" in sql
    assert "CAST(pid AS TEXT) AS pid" in sql
    assert "CAST(pid_new AS TEXT) AS pid_new" in sql


def test_template_lists_include_existing_mapping_counts(monkeypatch):
    repository = _repository()
    statements = []

    def fake_fetch_one(sql, params=()):
        statements.append(sql)
        return {"total": 0}

    def fake_fetch_all(sql, params=()):
        statements.append(sql)
        return []

    monkeypatch.setattr(repository, "_fetch_one", fake_fetch_one)
    monkeypatch.setattr(repository, "_fetch_all", fake_fetch_all)

    repository.list_html_templates()
    repository.list_standard_templates()

    sql = "\n".join(statements)
    assert "template_mapping_count" in sql
    assert "node_mapping_count" in sql
    assert "mapped_html_count" in sql


def test_existing_mappings_count_only_current_html_node_rows(monkeypatch):
    repository = _repository()
    statements = []

    def fake_fetch_all(sql, params=()):
        statements.append((sql, params))
        if 'doc_template_mapping' in sql:
            return [
                {'mapping_id': 'm1', 'standard_xml_id': 's1'},
                {'mapping_id': 'm2', 'standard_xml_id': 's2'},
            ]
        return [
            {'id': 'n1', 'standard_template_id': 's1'},
            {'id': 'n2', 'standard_template_id': 's1'},
            {'id': 'n3', 'standard_template_id': 's2'},
        ]

    monkeypatch.setattr(repository, '_fetch_all', fake_fetch_all)

    result = repository.get_existing_mappings('html-1')

    counts = {
        row['standard_xml_id']: row['node_mapping_count']
        for row in result['template_mappings']
    }
    assert counts == {'s1': 2, 's2': 1}
    assert result['node_mapping_count'] == 3
    assert all(params == ('html-1',) for _, params in statements)
    node_sql = next(sql for sql, _ in statements if 'doc_fhir_node_mapping' in sql)
    assert 'WHERE html_template_id = %s' in node_sql
    assert 'GROUP BY' not in node_sql.upper()


class _WriteCursor:
    def __init__(self, *, fail_node_insert=False):
        self.rows = []
        self.statements = []
        self.fail_node_insert = fail_node_insert

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=()):
        self.statements.append((sql, params))
        normalized = " ".join(sql.split())
        if "FROM \"sm_dmp\".\"doc_html_template\"" in normalized:
            self.rows = [{"template_id": "h1", "category_id": "hc1", "html_name": "HTML", "xml_version": "1"}]
        elif "FROM \"sm_dmp\".\"doc_standard_template\"" in normalized:
            self.rows = [{"id": "s1", "name": "V1", "category_id": "sc1", "category_name": "入院记录", "status": 1}]
        elif "FROM \"sm_dmp\".\"doc_standard_template_node\"" in normalized:
            self.rows = [{"id": "n1"}]
        elif "FROM \"sm_dmp\".\"doc_template_mapping\"" in normalized:
            self.rows = []
        elif "FROM \"sm_dmp\".\"doc_fhir_node_mapping\"" in normalized:
            self.rows = []
        else:
            self.rows = []
        if self.fail_node_insert and normalized.startswith('INSERT INTO "sm_dmp"."doc_fhir_node_mapping"'):
            raise RuntimeError("simulated insert failure")

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)


class _WriteConnection:
    def __init__(self, cursor):
        self.test_cursor = cursor
        self.commits = 0
        self.rollbacks = 0

    def set_session(self, **kwargs):
        self.session = kwargs

    def cursor(self, **kwargs):
        return self.test_cursor

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class _WritePool:
    def __init__(self, connection):
        self.connection = connection

    def getconn(self):
        return self.connection

    def putconn(self, connection):
        assert connection is self.connection


class _FixedIdProvider:
    def next_id(self):
        return 987654321


def test_reviewed_binding_write_includes_required_state_and_audit_fields(monkeypatch):
    repository = _repository()
    cursor = _WriteCursor()
    connection = _WriteConnection(cursor)
    monkeypatch.setattr(repository, "_get_pool", lambda: _WritePool(connection))
    monkeypatch.setenv("TEMPLATE_BINDING_AUDIT_USER", "binding-test")

    result = repository.save_reviewed_bindings(
        mapping_id="m1",
        html_template_id="h1",
        html_category_id="hc1",
        standard_template_id="s1",
        expected_update_time=None,
        node_mappings=[{
            "standard_node_id": "n1",
            "html_node_code": "code:S001",
            "html_node_id": "code:S001;S001",
            "mapping_values": "{主诉}",
        }],
        id_provider=_FixedIdProvider(),
    )

    sql = "\n".join(statement for statement, _ in cursor.statements)
    assert "mapping_state, switch_state" in sql
    assert "create_by, create_time, update_by, update_time" in sql
    assert 'CAST(m.standard_xml_id AS TEXT) AS standard_xml_id' in sql
    assert 'CAST(m.update_time AS TEXT) AS update_time' in sql
    assert 'CAST(m.create_time AS TEXT) AS create_time' in sql
    html_lock_sql = next(
        statement
        for statement, _ in cursor.statements
        if 'FROM "sm_dmp"."doc_html_template"' in statement and 'FOR UPDATE' in statement
    )
    mapping_lock_sql = next(
        statement
        for statement, _ in cursor.statements
        if 'SELECT CAST(m.mapping_id AS TEXT)' in statement
    )
    assert 'FOR UPDATE OF m' not in html_lock_sql
    assert 'FOR UPDATE OF m' in mapping_lock_sql
    assert any(987654321 in params for _, params in cursor.statements if params)
    assert result["node_inserted"] == 1
    assert connection.commits == 1
    assert connection.rollbacks == 0


def test_reviewed_binding_write_rolls_back_on_node_failure(monkeypatch):
    repository = _repository()
    cursor = _WriteCursor(fail_node_insert=True)
    connection = _WriteConnection(cursor)
    monkeypatch.setattr(repository, "_get_pool", lambda: _WritePool(connection))

    try:
        repository.save_reviewed_bindings(
            mapping_id="m1",
            html_template_id="h1",
            html_category_id="hc1",
            standard_template_id="s1",
            expected_update_time=None,
            node_mappings=[{
                "standard_node_id": "n1",
                "html_node_code": "code:S001",
                "html_node_id": "code:S001",
                "mapping_values": "{主诉}",
            }],
            id_provider=_FixedIdProvider(),
        )
    except TemplateBindingRepositoryError as exc:
        assert "simulated insert failure" in str(exc)
    else:
        raise AssertionError("write failure must be surfaced")

    assert connection.commits == 0
    assert connection.rollbacks == 1
