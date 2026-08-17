import json

import pytest

from microharness.template_binding.config import (
    TemplateBindingConfigurationError,
    build_database_config,
    load_database_config,
    save_database_config,
)


def _data(**overrides):
    result = {
        "host": "127.0.0.1",
        "port": 5432,
        "database": "dmp",
        "schema": "sm_dmp",
        "user": "reader",
        "password": "secret",
        "pool_min": 1,
        "pool_max": 4,
        "connect_timeout_seconds": 5,
        "statement_timeout_seconds": 20,
    }
    result.update(overrides)
    return result


def test_config_requires_database_and_user():
    with pytest.raises(TemplateBindingConfigurationError):
        build_database_config(_data(database=""))
    with pytest.raises(TemplateBindingConfigurationError):
        build_database_config(_data(user=""))


def test_public_config_never_returns_password():
    config = build_database_config(_data())

    public = config.to_public_dict()

    assert public["password"] == ""
    assert public["password_configured"] is True
    assert "secret" not in json.dumps(public)


def test_save_preserves_existing_password_when_form_leaves_it_blank(tmp_path, monkeypatch):
    path = tmp_path / "template_binding_database.json"
    for env_name in (
        "TEMPLATE_BINDING_DB_HOST",
        "TEMPLATE_BINDING_DB_PORT",
        "TEMPLATE_BINDING_DB_NAME",
        "TEMPLATE_BINDING_DB_SCHEMA",
        "TEMPLATE_BINDING_DB_USER",
        "TEMPLATE_BINDING_DB_PASSWORD",
    ):
        monkeypatch.delenv(env_name, raising=False)
    save_database_config(_data(), path)

    saved = save_database_config(_data(host="db.internal", password=""), path)

    assert saved.host == "db.internal"
    assert saved.password == "secret"
    assert load_database_config(path).password == "secret"


def test_environment_values_override_saved_config(tmp_path, monkeypatch):
    path = tmp_path / "template_binding_database.json"
    save_database_config(_data(), path)
    monkeypatch.setenv("TEMPLATE_BINDING_DB_HOST", "env-host")
    monkeypatch.setenv("TEMPLATE_BINDING_DB_PORT", "6432")

    config = load_database_config(path)

    assert config.host == "env-host"
    assert config.port == 6432
