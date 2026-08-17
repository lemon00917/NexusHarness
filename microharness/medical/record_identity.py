from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def record_identity_config(semantic: Mapping[str, Any] | None) -> dict[str, Any]:
    semantic = semantic if isinstance(semantic, Mapping) else {}
    presentation = semantic.get("presentation")
    presentation = presentation if isinstance(presentation, Mapping) else {}
    config = presentation.get("record_identity")
    config = config if isinstance(config, Mapping) else {}
    raw_fields = config.get("fields")
    if isinstance(raw_fields, str):
        raw_fields = [raw_fields]
    fields = raw_fields if isinstance(raw_fields, (list, tuple)) else []
    return {
        "label": str(config.get("label") or "").strip(),
        "fields": [str(field).strip() for field in fields if str(field).strip()],
    }


def resolve_record_identity(
    raw_fields: Mapping[str, Any] | None,
    semantic: Mapping[str, Any] | None,
) -> dict[str, str]:
    config = record_identity_config(semantic)
    if not config["label"] or not config["fields"]:
        return {}
    raw_fields = raw_fields if isinstance(raw_fields, Mapping) else {}
    casefolded = {str(key).casefold(): (str(key), value) for key, value in raw_fields.items()}
    for configured_field in config["fields"]:
        actual_field = configured_field
        value = raw_fields.get(configured_field)
        if value is None and configured_field.casefold() in casefolded:
            actual_field, value = casefolded[configured_field.casefold()]
        text = "" if value is None else str(value).strip()
        if text:
            return {
                "record_id": text,
                "record_id_label": config["label"],
                "record_id_field": actual_field,
            }
    return {}


def identity_from_binding(binding: Mapping[str, Any] | None) -> dict[str, str]:
    binding = binding if isinstance(binding, Mapping) else {}
    value = binding.get("record_id")
    record_id = "" if value is None else str(value).strip()
    if not record_id:
        return {}
    return {
        "record_id": record_id,
        "record_id_label": str(binding.get("record_id_label") or "").strip(),
        "record_id_field": str(binding.get("record_id_field") or "").strip(),
    }


def display_record_reference(fallback: str, record_id: str = "", label: str = "") -> str:
    record_id = str(record_id or "").strip()
    label = str(label or "").strip()
    if record_id:
        return f"{label}={record_id}" if label else record_id
    return str(fallback or "记录").strip() or "记录"
