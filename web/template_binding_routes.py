"""API and DMP configuration routes for the template-binding workbench."""

from __future__ import annotations

import os
import threading
from functools import partial
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field

from microharness.template_binding.config import (
    TemplateBindingConfigurationError,
    build_database_config,
    load_database_config,
    load_stored_database_config,
    save_database_config,
)
from microharness.template_binding.html_parser import HtmlTemplateDecodeError, parse_html_info
from microharness.template_binding.id_provider import IdProviderNotConfigured
from microharness.template_binding.persistence import (
    TemplateBindingCommitError,
    TemplateBindingCommitService,
)
from microharness.template_binding.repository import (
    TemplateBindingConflictError,
    TemplateBindingRepository,
    TemplateBindingRepositoryError,
)
from microharness.template_binding.standard_tree import StandardNodeTreeError, build_standard_tree
from microharness.template_binding.service import (
    TemplateBindingAnalysisError,
    TemplateBindingAnalysisService,
)


router = APIRouter(tags=["template-binding"])

_repository: TemplateBindingRepository | None = None
_repository_lock = threading.Lock()


class DmpDatabaseConfigPayload(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    host: str = Field(default="127.0.0.1", min_length=1, max_length=255)
    port: int = Field(default=5432, ge=1, le=65535)
    database: str = Field(min_length=1, max_length=255)
    schema_name: str = Field(default="sm_dmp", alias="schema", min_length=1, max_length=128)
    user: str = Field(min_length=1, max_length=255)
    password: str = Field(default="", max_length=2048)
    pool_min: int = Field(default=1, ge=1, le=100)
    pool_max: int = Field(default=10, ge=1, le=100)
    connect_timeout_seconds: int = Field(default=10, ge=1, le=300)
    statement_timeout_seconds: int = Field(default=30, ge=1, le=3600)
    clear_password: bool = False


class TemplateBindingAnalyzePayload(BaseModel):
    html_template_id: str = Field(min_length=1, max_length=255)
    html_category_id: str = Field(min_length=1, max_length=255)
    standard_template_id: str | None = Field(default=None, max_length=255)
    template_match_model: str | None = Field(default=None, max_length=255)
    node_match_model: str | None = Field(default=None, max_length=255)
    use_llm: bool = True
    existing_mapping_policy: str = Field(default="reference", max_length=32)


class TemplateBindingNodeCommitPayload(BaseModel):
    standard_node_id: str = Field(min_length=1, max_length=255)
    html_node_keys: list[str] = Field(min_length=1, max_length=50)


class TemplateBindingCommitPayload(BaseModel):
    html_template_id: str = Field(min_length=1, max_length=255)
    html_category_id: str = Field(min_length=1, max_length=255)
    standard_template_id: str = Field(min_length=1, max_length=255)
    expected_update_time: str | None = Field(default=None, max_length=128)
    node_mappings: list[TemplateBindingNodeCommitPayload] = Field(min_length=1, max_length=1000)


def _get_repository() -> TemplateBindingRepository:
    global _repository
    if _repository is None:
        with _repository_lock:
            if _repository is None:
                _repository = TemplateBindingRepository(load_database_config())
    return _repository


def _reset_repository() -> None:
    global _repository
    with _repository_lock:
        previous = _repository
        _repository = None
    if previous is not None:
        previous.close()


def _public_config() -> dict[str, Any]:
    try:
        result = load_database_config().to_public_dict()
        result["configured"] = True
        return result
    except TemplateBindingConfigurationError as exc:
        try:
            stored = load_stored_database_config()
        except TemplateBindingConfigurationError:
            stored = {}

        def effective(env_name: str, key: str, default: Any) -> Any:
            return os.getenv(env_name, stored.get(key, default))

        def safe_int(env_name: str, key: str, default: int) -> int:
            try:
                return int(effective(env_name, key, default))
            except (TypeError, ValueError):
                return default

        return {
            "type": "opengauss",
            "name": "DMP data source",
            "host": effective("TEMPLATE_BINDING_DB_HOST", "host", "127.0.0.1"),
            "port": safe_int("TEMPLATE_BINDING_DB_PORT", "port", 5432),
            "database": effective("TEMPLATE_BINDING_DB_NAME", "database", ""),
            "schema": effective("TEMPLATE_BINDING_DB_SCHEMA", "schema", "sm_dmp"),
            "user": effective("TEMPLATE_BINDING_DB_USER", "user", ""),
            "password": "",
            "password_configured": bool(
                effective("TEMPLATE_BINDING_DB_PASSWORD", "password", "")
            ),
            "pool_min": safe_int("TEMPLATE_BINDING_DB_POOL_MIN", "pool_min", 1),
            "pool_max": safe_int("TEMPLATE_BINDING_DB_POOL_MAX", "pool_max", 10),
            "connect_timeout_seconds": safe_int(
                "TEMPLATE_BINDING_DB_CONNECT_TIMEOUT", "connect_timeout_seconds", 10
            ),
            "statement_timeout_seconds": safe_int(
                "TEMPLATE_BINDING_DB_STATEMENT_TIMEOUT", "statement_timeout_seconds", 30
            ),
            "environment_overrides": sorted(
                key.removeprefix("TEMPLATE_BINDING_DB_").lower()
                for key in os.environ
                if key.startswith("TEMPLATE_BINDING_DB_")
            ),
            "configured": False,
            "error": str(exc),
        }


def _payload_config(payload: DmpDatabaseConfigPayload):
    data = payload.model_dump(exclude={"clear_password"}, by_alias=True)
    if payload.clear_password:
        data["password"] = ""
    elif not payload.password:
        try:
            data["password"] = load_database_config().password
        except TemplateBindingConfigurationError:
            data["password"] = load_stored_database_config().get("password", "")
    return build_database_config(data, use_environment=False)


async def _repository_call(method_name: str, *args: Any, **kwargs: Any) -> Any:
    try:
        repository = _get_repository()
        method: Callable[..., Any] = getattr(repository, method_name)
        return await run_in_threadpool(partial(method, *args, **kwargs))
    except TemplateBindingConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except TemplateBindingRepositoryError as exc:
        raise HTTPException(status_code=502, detail=f"DMP database query failed: {exc}") from exc


@router.get("/templates/template_binding_db.html", response_class=HTMLResponse)
async def template_binding_page() -> HTMLResponse:
    path = Path(__file__).parent / "templates" / "template_binding_db.html"
    if not path.exists():
        raise HTTPException(status_code=404, detail="template binding workbench not found")
    return HTMLResponse(path.read_text(encoding="utf-8"))


@router.get("/template-binding")
async def redirect_template_binding():
    from fastapi.responses import RedirectResponse

    return RedirectResponse(url="/templates/template_binding_db.html")


@router.get("/template_binding_db.html")
async def redirect_legacy_template_binding_page():
    """Keep the filename-style workbench URL compatible with existing links."""
    from fastapi.responses import RedirectResponse

    return RedirectResponse(url="/templates/template_binding_db.html")


@router.get("/api/template-binding/database/config")
async def get_dmp_database_config() -> dict[str, Any]:
    return _public_config()


@router.post("/api/template-binding/database/config")
async def save_dmp_database_config(payload: DmpDatabaseConfigPayload) -> dict[str, Any]:
    try:
        data = payload.model_dump(exclude={"clear_password"}, by_alias=True)
        if payload.clear_password:
            data["password"] = ""
        config = await run_in_threadpool(
            partial(
                save_database_config,
                data,
                preserve_password=not payload.clear_password,
            )
        )
        await run_in_threadpool(_reset_repository)
        result = config.to_public_dict()
        result.update({"configured": True, "status": "saved", "effective_immediately": True})
        return result
    except TemplateBindingConfigurationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/template-binding/database/test")
async def test_saved_dmp_database_config() -> dict[str, Any]:
    result = await _repository_call("test_connection")
    return {"ok": True, **result}


@router.post("/api/template-binding/database/test")
async def test_dmp_database_config(payload: DmpDatabaseConfigPayload) -> dict[str, Any]:
    try:
        config = _payload_config(payload)
        repository = TemplateBindingRepository(config)
        try:
            result = await run_in_threadpool(repository.test_connection)
        finally:
            await run_in_threadpool(repository.close)
        return {"ok": True, **result}
    except (TemplateBindingConfigurationError, TemplateBindingRepositoryError) as exc:
        raise HTTPException(status_code=400, detail=f"DMP database connection failed: {exc}") from exc


@router.get("/api/template-binding/html/categories")
async def list_html_categories(
    search: str = "",
    parent_id: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    return await _repository_call(
        "list_html_categories", search=search, parent_id=parent_id, page=page, page_size=page_size
    )


@router.get("/api/template-binding/html/templates")
async def list_html_templates(
    category_id: str | None = None,
    search: str = "",
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    return await _repository_call(
        "list_html_templates", category_id=category_id, search=search, page=page, page_size=page_size
    )


@router.get("/api/template-binding/html/templates/{template_id}")
async def get_html_template(
    template_id: str,
    category_id: str = Query(min_length=1),
) -> dict[str, Any]:
    result = await _repository_call("get_html_template", template_id, category_id)
    if result is None:
        raise HTTPException(status_code=404, detail="HTML template not found for the composite key")
    return result


@router.get("/api/template-binding/html/templates/{template_id}/nodes")
async def get_html_template_nodes(
    template_id: str,
    category_id: str = Query(min_length=1),
    include_html: bool = False,
) -> dict[str, Any]:
    template = await _repository_call("get_html_template", template_id, category_id, include_html=True)
    if template is None:
        raise HTTPException(status_code=404, detail="HTML template not found for the composite key")
    html_info = template.pop("html_info", None)
    if not html_info:
        raise HTTPException(status_code=422, detail="HTML template has no html_info")
    try:
        parsed = await run_in_threadpool(partial(parse_html_info, html_info, include_html=include_html))
    except HtmlTemplateDecodeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"template": template, **parsed}


@router.get("/api/template-binding/standard/categories")
async def list_standard_categories(
    search: str = "",
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=100, ge=1, le=200),
) -> dict[str, Any]:
    return await _repository_call(
        "list_standard_categories", search=search, page=page, page_size=page_size
    )


@router.get("/api/template-binding/standard/templates")
async def list_standard_templates(
    category_id: str | None = None,
    search: str = "",
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=100, ge=1, le=200),
) -> dict[str, Any]:
    return await _repository_call(
        "list_standard_templates", category_id=category_id, search=search, page=page, page_size=page_size
    )


@router.get("/api/template-binding/standard/templates/{template_id}/nodes")
async def get_standard_template_nodes(template_id: str) -> dict[str, Any]:
    template = await _repository_call("get_standard_template", template_id)
    if template is None:
        raise HTTPException(status_code=404, detail="standard clinical template not found")
    rows = await _repository_call("list_standard_nodes", template_id)
    try:
        tree = await run_in_threadpool(partial(build_standard_tree, rows, template_id))
    except StandardNodeTreeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"template": template, **tree}


@router.get("/api/template-binding/mappings")
async def get_existing_mappings(
    html_template_id: str = Query(min_length=1),
    html_category_id: str | None = None,
) -> dict[str, Any]:
    result = await _repository_call("get_existing_mappings", html_template_id)
    result["html_category_id"] = html_category_id
    return result


@router.post("/api/template-binding/analyze")
async def analyze_template_binding(payload: TemplateBindingAnalyzePayload) -> dict[str, Any]:
    """Build constrained template and node recommendations without writing mappings."""
    try:
        repository = _get_repository()
        service = TemplateBindingAnalysisService(repository)
        return await run_in_threadpool(
            partial(
                service.analyze,
                html_template_id=payload.html_template_id,
                html_category_id=payload.html_category_id,
                standard_template_id=payload.standard_template_id,
                template_match_model=payload.template_match_model,
                node_match_model=payload.node_match_model,
                use_llm=payload.use_llm,
                existing_mapping_policy=payload.existing_mapping_policy,
            )
        )
    except TemplateBindingConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except TemplateBindingRepositoryError as exc:
        raise HTTPException(status_code=502, detail=f"DMP database query failed: {exc}") from exc
    except TemplateBindingAnalysisError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/api/template-binding/commit")
async def commit_template_binding(payload: TemplateBindingCommitPayload) -> dict[str, Any]:
    """Persist user-reviewed Stage4 mappings using transactional PATCH semantics."""
    try:
        repository = _get_repository()
        service = TemplateBindingCommitService(repository)
        return await run_in_threadpool(
            partial(
                service.commit,
                html_template_id=payload.html_template_id,
                html_category_id=payload.html_category_id,
                standard_template_id=payload.standard_template_id,
                expected_update_time=payload.expected_update_time,
                node_mappings=[item.model_dump() for item in payload.node_mappings],
            )
        )
    except TemplateBindingConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except TemplateBindingCommitError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except IdProviderNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except TemplateBindingConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except TemplateBindingRepositoryError as exc:
        raise HTTPException(status_code=502, detail=f"DMP database write failed: {exc}") from exc
