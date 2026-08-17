"""Independent openGauss configuration for template binding."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class TemplateBindingConfigurationError(RuntimeError):
    """Raised when the template-binding database is misconfigured."""


CONFIG_PATH = Path(__file__).parents[2] / "configs" / "template_binding_database.json"

_ENVIRONMENT_FIELDS = {
    "TEMPLATE_BINDING_DB_HOST": "host",
    "TEMPLATE_BINDING_DB_PORT": "port",
    "TEMPLATE_BINDING_DB_NAME": "database",
    "TEMPLATE_BINDING_DB_SCHEMA": "schema",
    "TEMPLATE_BINDING_DB_USER": "user",
    "TEMPLATE_BINDING_DB_PASSWORD": "password",
    "TEMPLATE_BINDING_DB_POOL_MIN": "pool_min",
    "TEMPLATE_BINDING_DB_POOL_MAX": "pool_max",
    "TEMPLATE_BINDING_DB_CONNECT_TIMEOUT": "connect_timeout_seconds",
    "TEMPLATE_BINDING_DB_STATEMENT_TIMEOUT": "statement_timeout_seconds",
}


@dataclass(frozen=True)
class TemplateBindingDatabaseConfig:
    host: str
    port: int
    database: str
    schema: str
    user: str
    password: str
    pool_min: int = 1
    pool_max: int = 10
    connect_timeout_seconds: int = 10
    statement_timeout_seconds: int = 30

    @property
    def connection_kwargs(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "port": self.port,
            "dbname": self.database,
            "user": self.user,
            "password": self.password,
            "connect_timeout": self.connect_timeout_seconds,
        }

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "type": "opengauss",
            "name": "DMP data source",
            "host": self.host,
            "port": self.port,
            "database": self.database,
            "schema": self.schema,
            "user": self.user,
            "password": "",
            "password_configured": bool(self.password),
            "pool_min": self.pool_min,
            "pool_max": self.pool_max,
            "connect_timeout_seconds": self.connect_timeout_seconds,
            "statement_timeout_seconds": self.statement_timeout_seconds,
            "environment_overrides": sorted(
                field for env_name, field in _ENVIRONMENT_FIELDS.items() if os.getenv(env_name) is not None
            ),
        }


def _value(data: dict[str, Any], env_name: str, key: str, default: Any = None) -> Any:
    env_value = os.getenv(env_name)
    return env_value if env_value is not None else data.get(key, default)


def load_stored_database_config(path: str | Path | None = None) -> dict[str, Any]:
    config_path = Path(path) if path else CONFIG_PATH
    if not config_path.exists():
        return {}
    try:
        loaded = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TemplateBindingConfigurationError(f"invalid DMP database config: {exc}") from exc
    if not isinstance(loaded, dict):
        raise TemplateBindingConfigurationError("invalid DMP database config: root must be an object")
    return loaded


def load_database_config(path: str | Path | None = None) -> TemplateBindingDatabaseConfig:
    data = load_stored_database_config(path)
    return build_database_config(data, use_environment=True)


def build_database_config(
    data: dict[str, Any],
    *,
    use_environment: bool = False,
) -> TemplateBindingDatabaseConfig:
    def value(env_name: str, key: str, default: Any = None) -> Any:
        return _value(data, env_name, key, default) if use_environment else data.get(key, default)

    try:
        host = str(value("TEMPLATE_BINDING_DB_HOST", "host", "127.0.0.1")).strip()
        port = int(value("TEMPLATE_BINDING_DB_PORT", "port", 5432))
        database = str(value("TEMPLATE_BINDING_DB_NAME", "database", "")).strip()
        schema = str(value("TEMPLATE_BINDING_DB_SCHEMA", "schema", "sm_dmp")).strip()
        user = str(value("TEMPLATE_BINDING_DB_USER", "user", "")).strip()
        password = str(value("TEMPLATE_BINDING_DB_PASSWORD", "password", ""))
        pool_min = int(value("TEMPLATE_BINDING_DB_POOL_MIN", "pool_min", 1))
        pool_max = int(value("TEMPLATE_BINDING_DB_POOL_MAX", "pool_max", 10))
        connect_timeout = int(value("TEMPLATE_BINDING_DB_CONNECT_TIMEOUT", "connect_timeout_seconds", 10))
        statement_timeout = int(
            value("TEMPLATE_BINDING_DB_STATEMENT_TIMEOUT", "statement_timeout_seconds", 30)
        )
    except (TypeError, ValueError) as exc:
        raise TemplateBindingConfigurationError(f"invalid DMP database numeric setting: {exc}") from exc

    if not database or not user:
        raise TemplateBindingConfigurationError("DMP database is not configured; database and user are required")
    if not host or not 1 <= port <= 65535:
        raise TemplateBindingConfigurationError("DMP database host and port are invalid")
    if pool_min < 1 or pool_max < pool_min:
        raise TemplateBindingConfigurationError("pool_max must be >= pool_min >= 1")
    if connect_timeout < 1 or statement_timeout < 1:
        raise TemplateBindingConfigurationError("database timeouts must be positive")
    if not schema or not schema.replace("_", "").isalnum():
        raise TemplateBindingConfigurationError("invalid DMP database schema")

    return TemplateBindingDatabaseConfig(
        host=host,
        port=port,
        database=database,
        schema=schema,
        user=user,
        password=password,
        pool_min=pool_min,
        pool_max=pool_max,
        connect_timeout_seconds=connect_timeout,
        statement_timeout_seconds=statement_timeout,
    )


def save_database_config(
    data: dict[str, Any],
    path: str | Path | None = None,
    *,
    preserve_password: bool = True,
) -> TemplateBindingDatabaseConfig:
    config_path = Path(path) if path else CONFIG_PATH
    stored = load_stored_database_config(config_path)
    normalized = {
        "type": "opengauss",
        "host": data.get("host", "127.0.0.1"),
        "port": data.get("port", 5432),
        "database": data.get("database", ""),
        "schema": data.get("schema", "sm_dmp"),
        "user": data.get("user", ""),
        "password": data.get("password", ""),
        "pool_min": data.get("pool_min", 1),
        "pool_max": data.get("pool_max", 10),
        "connect_timeout_seconds": data.get("connect_timeout_seconds", 10),
        "statement_timeout_seconds": data.get("statement_timeout_seconds", 30),
    }
    if preserve_password and not normalized["password"]:
        normalized["password"] = stored.get("password", "")
    build_database_config(normalized, use_environment=False)

    config_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = config_path.with_suffix(config_path.suffix + ".tmp")
    temporary_path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary_path.replace(config_path)
    return load_database_config(config_path)
