import asyncio

from microharness.template_binding.config import TemplateBindingConfigurationError
from web import template_binding_routes


def test_workbench_exposes_filename_style_compatibility_route():
    paths = {route.path for route in template_binding_routes.router.routes}

    assert "/templates/template_binding_db.html" in paths
    assert "/template-binding" in paths
    assert "/template_binding_db.html" in paths


def test_public_config_survives_invalid_numeric_environment(monkeypatch):
    monkeypatch.setattr(
        template_binding_routes,
        "load_database_config",
        lambda: (_ for _ in ()).throw(TemplateBindingConfigurationError("invalid port")),
    )
    monkeypatch.setattr(
        template_binding_routes,
        "load_stored_database_config",
        lambda: {"database": "dmp", "user": "reader"},
    )
    monkeypatch.setenv("TEMPLATE_BINDING_DB_PORT", "not-a-number")

    result = template_binding_routes._public_config()

    assert result["configured"] is False
    assert result["port"] == 5432
    assert result["password"] == ""
    assert result["error"] == "invalid port"


def test_public_config_survives_invalid_stored_json(monkeypatch):
    monkeypatch.setattr(
        template_binding_routes,
        "load_database_config",
        lambda: (_ for _ in ()).throw(TemplateBindingConfigurationError("invalid json")),
    )
    monkeypatch.setattr(
        template_binding_routes,
        "load_stored_database_config",
        lambda: (_ for _ in ()).throw(TemplateBindingConfigurationError("invalid json")),
    )

    result = template_binding_routes._public_config()

    assert result["configured"] is False
    assert result["database"] == ""
    assert result["port"] == 5432


def test_analyze_route_is_read_only_and_forwards_composite_key(monkeypatch):
    captured = {}

    class FakeService:
        def __init__(self, repository):
            captured["repository"] = repository

        def analyze(self, **kwargs):
            captured.update(kwargs)
            return {"read_only": True, "status": "COMPLETED"}

    repository = object()
    monkeypatch.setattr(template_binding_routes, "_get_repository", lambda: repository)
    monkeypatch.setattr(template_binding_routes, "TemplateBindingAnalysisService", FakeService)
    payload = template_binding_routes.TemplateBindingAnalyzePayload(
        html_template_id="h1",
        html_category_id="c1",
        use_llm=False,
        template_match_model="qwen2.5:3b",
        node_match_model="deepseek-r1:1.5b",
    )

    result = asyncio.run(template_binding_routes.analyze_template_binding(payload))

    assert result["read_only"] is True
    assert captured["html_template_id"] == "h1"
    assert captured["html_category_id"] == "c1"
    assert captured["use_llm"] is False
    assert captured["template_match_model"] == "qwen2.5:3b"
    assert captured["node_match_model"] == "deepseek-r1:1.5b"


def test_commit_route_forwards_reviewed_node_keys(monkeypatch):
    captured = {}

    class FakeCommitService:
        def __init__(self, repository):
            captured["repository"] = repository

        def commit(self, **kwargs):
            captured.update(kwargs)
            return {"saved": True, "node_inserted": 1}

    repository = object()
    monkeypatch.setattr(template_binding_routes, "_get_repository", lambda: repository)
    monkeypatch.setattr(template_binding_routes, "TemplateBindingCommitService", FakeCommitService)
    payload = template_binding_routes.TemplateBindingCommitPayload(
        html_template_id="h1",
        html_category_id="hc1",
        standard_template_id="s1",
        node_mappings=[{"standard_node_id": "n1", "html_node_keys": ["node-1"]}],
    )

    result = asyncio.run(template_binding_routes.commit_template_binding(payload))

    assert result["saved"] is True
    assert captured["standard_template_id"] == "s1"
    assert captured["node_mappings"] == [
        {"standard_node_id": "n1", "html_node_keys": ["node-1"]}
    ]
