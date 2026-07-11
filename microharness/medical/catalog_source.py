"""Load medical document metadata from local config or the CDR metadata API."""

from __future__ import annotations

import copy
import json
import os
import re
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).parent.parent.parent
LOCAL_CATALOG_PATH = PROJECT_ROOT / "configs" / "medical_catalog.json"
SOURCE_CONFIG_PATH = PROJECT_ROOT / "configs" / "medical_catalog_source.json"
DEFAULT_EXTERNAL_URL = "http://localhost:8006/cdr-api/standard/doc/template/node/customselect"
VALID_SOURCES = {"local", "external"}


def load_source_config() -> dict[str, str]:
    """Return validated source settings, defaulting to the local catalog."""
    settings: dict[str, Any] = {}
    if SOURCE_CONFIG_PATH.exists():
        try:
            loaded = json.loads(SOURCE_CONFIG_PATH.read_text(encoding="utf-8-sig"))
            if isinstance(loaded, dict):
                settings = loaded
        except (OSError, json.JSONDecodeError):
            settings = {}

    source = str(settings.get("source") or "local").strip().lower()
    if source not in VALID_SOURCES:
        source = "local"
    external_url = str(
        os.environ.get("MEDICAL_CATALOG_EXTERNAL_URL")
        or settings.get("external_url")
        or DEFAULT_EXTERNAL_URL
    ).strip()
    return {"source": source, "external_url": external_url}


def save_source_config(source: str, external_url: str | None = None) -> dict[str, str]:
    """Persist the selected catalog source."""
    normalized_source = str(source or "").strip().lower()
    if normalized_source not in VALID_SOURCES:
        raise ValueError(f"Unsupported medical catalog source: {source}")
    current = load_source_config()
    settings = {
        "source": normalized_source,
        "external_url": str(external_url or current["external_url"]).strip(),
    }
    SOURCE_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    SOURCE_CONFIG_PATH.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return settings


def load_local_catalog(fallback_catalog: dict | None = None) -> dict:
    """Load the editable local semantic catalog."""
    if LOCAL_CATALOG_PATH.exists():
        try:
            loaded = json.loads(LOCAL_CATALOG_PATH.read_text(encoding="utf-8-sig"))
            if isinstance(loaded, dict) and loaded:
                return loaded
        except (OSError, json.JSONDecodeError):
            pass
    return copy.deepcopy(fallback_catalog or {})


def fetch_external_catalog(url: str, timeout: float = 5.0) -> dict:
    """Fetch and validate the document metadata returned by the CDR API."""
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise ValueError("外部元数据接口返回值不是JSON对象")
    if payload.get("success") is False:
        raise ValueError(str(payload.get("msg") or "外部元数据接口返回失败"))
    code = str(payload.get("code") or "200")
    if code != "200":
        raise ValueError(str(payload.get("msg") or f"外部元数据接口状态码异常: {code}"))
    data = payload.get("data")
    if not isinstance(data, dict) or not data:
        raise ValueError("外部元数据接口未返回有效data")
    return data


def _canonical_section_name(name: str) -> str:
    """Normalize common display-name decorations for semantic metadata matching."""
    value = str(name or "").strip()
    value = re.split(r"[：:]", value, maxsplit=1)[0]
    value = re.sub(r"[（(].*?[）)]", "", value)
    if value.endswith("日期时间"):
        value = value[:-2]
    return re.sub(r"\s+", "", value)


def _find_local_section(external_name: str, local_sections: list[dict]) -> dict | None:
    external_key = _canonical_section_name(external_name)
    for section in local_sections:
        names = [section.get("name"), *(section.get("aliases") or [])]
        if any(_canonical_section_name(name) == external_key for name in names if name):
            return section
    return None


def merge_external_with_local(external_catalog: dict, local_catalog: dict) -> dict:
    """Use external base metadata while retaining local-only routing semantics."""
    merged_catalog: dict[str, dict] = {}

    for doc_name, external_doc in external_catalog.items():
        if not isinstance(external_doc, dict):
            continue
        local_doc = local_catalog.get(doc_name, {})
        merged_doc = copy.deepcopy(local_doc) if isinstance(local_doc, dict) else {}
        for key, value in external_doc.items():
            if key != "sections":
                merged_doc[key] = copy.deepcopy(value)

        local_sections = (
            local_doc.get("sections", []) if isinstance(local_doc, dict) else []
        )
        local_sections = [s for s in local_sections if isinstance(s, dict)]
        matched_local_ids: set[int] = set()
        merged_sections: list[dict] = []

        for external_section in external_doc.get("sections", []) or []:
            if not isinstance(external_section, dict):
                continue
            external_name = str(external_section.get("name") or "").strip()
            local_section = _find_local_section(external_name, local_sections)
            merged_section = copy.deepcopy(local_section or {})
            merged_section.update(copy.deepcopy(external_section))

            if local_section is not None:
                matched_local_ids.add(id(local_section))
                local_name = str(local_section.get("name") or "").strip()
                if local_name and local_name != external_name:
                    aliases = list(merged_section.get("aliases") or [])
                    if local_name not in aliases:
                        aliases.append(local_name)
                    merged_section["aliases"] = aliases
            merged_sections.append(merged_section)

        for local_section in local_sections:
            if id(local_section) not in matched_local_ids:
                merged_sections.append(copy.deepcopy(local_section))

        merged_doc["sections"] = merged_sections
        merged_catalog[str(doc_name)] = merged_doc

    for doc_name, local_doc in local_catalog.items():
        if doc_name not in merged_catalog:
            merged_catalog[doc_name] = copy.deepcopy(local_doc)

    return merged_catalog


def load_effective_catalog(fallback_catalog: dict | None = None) -> tuple[dict, dict]:
    """Load the selected catalog and return it with observable source status."""
    settings = load_source_config()
    local_catalog = load_local_catalog(fallback_catalog)
    status = {
        "configured_source": settings["source"],
        "effective_source": "local",
        "external_url": settings["external_url"],
        "fallback": False,
        "error": "",
        "loaded_at": datetime.now(timezone.utc).isoformat(),
    }

    if settings["source"] == "local":
        status["document_count"] = len(local_catalog)
        return local_catalog, status

    try:
        external_catalog = fetch_external_catalog(settings["external_url"])
        effective = merge_external_with_local(external_catalog, local_catalog)
        status["effective_source"] = "external"
        status["document_count"] = len(effective)
        return effective, status
    except Exception as exc:
        status.update(
            {
                "fallback": True,
                "error": str(exc),
                "document_count": len(local_catalog),
            }
        )
        return local_catalog, status
