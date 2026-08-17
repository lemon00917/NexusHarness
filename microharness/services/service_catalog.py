"""
External Service Catalog
=========================
Scans skills/ directory for SKILL.md files with API metadata.
Each skill with `metadata.api` becomes an external data source.
"""

import json
import re
import yaml
from pathlib import Path
from typing import Any, Dict, List, Optional

_SKILLS_DIR = Path(__file__).parent.parent.parent / "skills"
_CONFIG_PATH = Path(__file__).parent.parent.parent / "configs" / "external_services.json"

# Shared base URL for external APIs
_BASE_URL = "http://43.143.68.242:9090/emviewdoctor/hdc/"

_SERVICE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}
_TEMPORAL_FILTER_MODES = {"generic", "domain"}


def _contract_message(code: str, field: str, message: str) -> dict:
    return {"code": code, "field": field, "message": message}


def validate_service_contract(
    service_id: str,
    service: dict,
    *,
    require_semantic: bool = False,
) -> dict:
    """Validate one external-service registration without disabling it.

    SKILL.md-backed services use ``require_semantic=True`` and must expose the
    complete machine contract. Config-only legacy services stay compatible:
    missing semantic fields are warnings so existing deployments keep running.
    """
    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []

    if not isinstance(service, dict):
        errors.append(_contract_message(
            "SERVICE_NOT_OBJECT", "service", "Service registration must be an object."
        ))
        return {
            "valid": False,
            "level": "invalid",
            "errors": errors,
            "warnings": warnings,
            "normalized": {
                "source_kind": "service",
                "temporal_filter_mode": "generic",
                "record_type": "",
            },
        }

    normalized_id = str(service_id or "").strip()
    if not _SERVICE_ID_PATTERN.fullmatch(normalized_id):
        errors.append(_contract_message(
            "INVALID_SERVICE_ID",
            "service_id",
            "Service ID must use lowercase letters, digits, dots, underscores, or hyphens.",
        ))

    if not str(service.get("url") or "").strip():
        errors.append(_contract_message("MISSING_URL", "url", "Service URL is required."))

    method = str(service.get("method") or "POST").strip().upper()
    if method not in _HTTP_METHODS:
        errors.append(_contract_message(
            "INVALID_HTTP_METHOD", "method", f"Unsupported HTTP method: {method or '<empty>'}."
        ))

    label = str(service.get("label") or service.get("display_name") or "").strip()
    if not label:
        target = errors if require_semantic else warnings
        target.append(_contract_message(
            "MISSING_LABEL", "label", "A stable display label is recommended."
        ))

    triggers = service.get("triggers")
    if not isinstance(triggers, list) or any(not isinstance(item, str) for item in triggers):
        target = errors if require_semantic else warnings
        target.append(_contract_message(
            "INVALID_TRIGGERS", "triggers", "Triggers must be a list of strings."
        ))

    request_map = service.get("request_map")
    if not isinstance(request_map, dict):
        target = errors if require_semantic else warnings
        target.append(_contract_message(
            "INVALID_REQUEST_MAP", "request_map", "Request mapping must be an object."
        ))

    semantic = service.get("semantic")
    if not isinstance(semantic, dict) or not semantic:
        target = errors if require_semantic else warnings
        target.append(_contract_message(
            "MISSING_SEMANTIC_CONTRACT",
            "semantic",
            "Semantic metadata is required for generic routing and evidence handling.",
        ))
        semantic = {}

    for field in ("entity_type", "domain"):
        if not str(semantic.get(field) or "").strip():
            target = errors if require_semantic else warnings
            target.append(_contract_message(
                f"MISSING_{field.upper()}",
                f"semantic.{field}",
                f"semantic.{field} must be a non-empty string.",
            ))

    evidence_types = semantic.get("evidence_types")
    valid_evidence_types = (
        isinstance(evidence_types, list)
        and bool(evidence_types)
        and all(isinstance(item, str) and item.strip() for item in evidence_types)
    )
    if not valid_evidence_types:
        target = errors if require_semantic else warnings
        target.append(_contract_message(
            "INVALID_EVIDENCE_TYPES",
            "semantic.evidence_types",
            "semantic.evidence_types must be a non-empty list of strings.",
        ))

    temporal_filter_mode = str(
        semantic.get("temporal_filter_mode") or "generic"
    ).strip().lower()
    if temporal_filter_mode not in _TEMPORAL_FILTER_MODES:
        errors.append(_contract_message(
            "INVALID_TEMPORAL_FILTER_MODE",
            "semantic.temporal_filter_mode",
            "Temporal filter mode must be 'generic' or 'domain'.",
        ))

    presentation = semantic.get("presentation", {})
    if presentation is None:
        presentation = {}
    if not isinstance(presentation, dict):
        errors.append(_contract_message(
            "INVALID_PRESENTATION",
            "semantic.presentation",
            "Presentation metadata must be an object.",
        ))
        presentation = {}
    record_type = str(
        presentation.get("record_type") or semantic.get("record_type") or ""
    ).strip()
    if "record_type" in presentation and not record_type:
        errors.append(_contract_message(
            "INVALID_RECORD_TYPE",
            "semantic.presentation.record_type",
            "Presentation record_type must be a non-empty string.",
        ))

    record_identity = presentation.get("record_identity")
    if record_identity is not None and not isinstance(record_identity, dict):
        errors.append(_contract_message(
            "INVALID_RECORD_IDENTITY",
            "semantic.presentation.record_identity",
            "Record identity metadata must be an object.",
        ))
        record_identity = None
    if isinstance(record_identity, dict):
        identity_label = str(record_identity.get("label") or "").strip()
        identity_fields = record_identity.get("fields")
        valid_fields = isinstance(identity_fields, list) and bool(identity_fields) and all(
            isinstance(field, str) and field.strip() for field in identity_fields
        )
        if not identity_label or not valid_fields:
            errors.append(_contract_message(
                "INVALID_RECORD_IDENTITY",
                "semantic.presentation.record_identity",
                "Record identity requires a non-empty label and fields list.",
            ))
    capabilities = semantic.get("evidence_capabilities")
    if capabilities is not None and not isinstance(capabilities, dict):
        errors.append(_contract_message(
            "INVALID_EVIDENCE_CAPABILITIES",
            "semantic.evidence_capabilities",
            "Evidence capabilities must be an object.",
        ))

    level = "invalid" if errors else ("complete" if not warnings else "compatible")
    return {
        "valid": not errors,
        "level": level,
        "errors": errors,
        "warnings": warnings,
        "normalized": {
            "source_kind": "service",
            "temporal_filter_mode": (
                temporal_filter_mode
                if temporal_filter_mode in _TEMPORAL_FILTER_MODES
                else "generic"
            ),
            "record_type": record_type,
            "entity_type": str(semantic.get("entity_type") or "").strip(),
            "domain": str(semantic.get("domain") or "").strip(),
            "evidence_types": (
                [str(item).strip() for item in evidence_types]
                if valid_evidence_types
                else []
            ),
        },
    }


def validate_service_catalog(
    services: dict,
    *,
    strict_service_ids: set[str] | None = None,
) -> dict:
    """Return aggregate registration health for an arbitrary service catalog."""
    strict_ids = set(strict_service_ids or set())
    reports = {}
    for service_id, service in (services or {}).items():
        if service_id == "base_url":
            continue
        reports[str(service_id)] = validate_service_contract(
            str(service_id),
            service,
            require_semantic=str(service_id) in strict_ids,
        )
    counts = {level: 0 for level in ("complete", "compatible", "invalid")}
    for report in reports.values():
        counts[report["level"]] += 1
    return {
        "valid": counts["invalid"] == 0,
        "counts": counts,
        "services": reports,
    }


def _attach_service_contracts(services: dict, strict_service_ids: set[str]) -> dict:
    reports = validate_service_catalog(services, strict_service_ids=strict_service_ids)
    for service_id, report in reports["services"].items():
        service = services.get(service_id)
        if isinstance(service, dict):
            service["_contract"] = report
    return services


def _load_config() -> dict:
    """Load external service config."""
    try:
        if _CONFIG_PATH.exists():
            cfg = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
            return cfg if isinstance(cfg, dict) else {}
    except Exception:
        pass
    return {}


def _parse_skill_md(path: Path) -> Optional[dict]:
    """Parse SKILL.md YAML frontmatter and body metadata."""
    text = path.read_text(encoding="utf-8")
    m = re.match(r'^---\s*\n(.*?)\n---', text, re.DOTALL)
    if not m:
        return None
    try:
        result = yaml.safe_load(m.group(1))
    except Exception:
        return None
    # Parse field label mappings from body (e.g. "encTypeDesc(就诊类型)")
    result["_field_labels"] = _parse_field_labels(text)
    # Parse H1 title as display label
    parts = text.split("---", 2)
    if len(parts) >= 3:
        h1_match = re.search(r'^#\s+(.+)$', parts[2], re.MULTILINE)
        if h1_match:
            result["_h1_label"] = h1_match.group(1).strip()
    return result


def _parse_field_labels(md_text: str) -> dict:
    """Extract field→Chinese label mappings from SKILL.md body.

    Matches patterns like: orderName(药物名称), encStartDate(入院日期)
    Found in the '## 返回字段' or '返回字段' section.
    Returns dict: {'orderName': '药物名称', 'encStartDate': '入院日期', ...}
    """
    labels = {}
    # Match: camelCase_or_underscore_field(中文标签)
    # The Chinese label may contain parentheses in rare cases, so we use a
    # non-greedy match with balanced-parens approximation
    def is_field_name(s: str) -> bool:
        return bool(re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', s or ""))

    pat = re.compile(r'([A-Za-z_][A-Za-z0-9_]*|[^,\n，、：:\-*\s()]{1,30})\(([^)]+)\)')
    for m in pat.finditer(md_text):
        left = m.group(1).strip()
        right = m.group(2).strip()
        if is_field_name(left):
            eng, chn = left, right
        elif is_field_name(right):
            eng, chn = right, left
        else:
            continue
        # Skip if it looks like a URL or path, not a field mapping
        if eng in ('http', 'https', 'url', 'api', 'id', 'ID'):
            continue
        if chn and len(chn) < 30 and len(eng) > 1:
            labels[eng] = chn
    return labels


def _load_base_url() -> str:
    """Load base_url from config file, fallback to default."""
    cfg = _load_config()
    url = cfg.get("base_url", "").strip()
    if url:
        return url
    return _BASE_URL


def save_base_url(url: str) -> None:
    """Save base_url to config file."""
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    cfg = {}
    if _CONFIG_PATH.exists():
        try:
            cfg = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    cfg["base_url"] = url
    _CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def load_services() -> dict:
    """Scan skills directory for API-enabled skills."""
    services = {"base_url": _load_base_url()}
    cfg = _load_config()
    skill_service_ids: set[str] = set()

    if not _SKILLS_DIR.exists():
        merged = _merge_config_services(services, cfg)
        return _attach_service_contracts(merged, skill_service_ids)

    for skill_dir in _SKILLS_DIR.iterdir():
        if not skill_dir.is_dir():
            continue
        md_file = skill_dir / "SKILL.md"
        if not md_file.exists():
            continue

        fm = _parse_skill_md(md_file)
        if not fm:
            continue

        metadata = fm.get("metadata", {})
        api_cfg = metadata.get("api", {})
        if not api_cfg or not api_cfg.get("url"):
            continue

        name = fm.get("name", skill_dir.name)
        skill_service_ids.add(str(name))
        triggers_raw = metadata.get("triggers", [])
        triggers = triggers_raw if isinstance(triggers_raw, list) else []

        # Chinese display label from H1 title (e.g. "# 诊断查询" → "诊断查询")
        label = fm.get("_h1_label", fm.get("description", name))

        # Field name → Chinese label mappings from SKILL.md body
        # e.g. {"encTypeDesc": "就诊类型", "encStartDate": "入院日期"}
        field_labels = fm.get("_field_labels", {})

        services[name] = {
            "name": fm.get("description", name),
            "label": label,
            "field_labels": field_labels,
            "url": api_cfg.get("url", ""),
            "method": api_cfg.get("method", "POST"),
            "description": fm.get("description", ""),
            "semantic": metadata.get("semantic", {}),
            "triggers": triggers,
            "request_wrapper": api_cfg.get("request_wrapper", ""),
            "request_map": api_cfg.get("request_map", {}),
            "returns": api_cfg.get("returns", ""),
            "keep_fields": api_cfg.get("keep_fields"),
            "rec_prefix": api_cfg.get("rec_prefix", "记录"),
            "merge": api_cfg.get("merge", {}),
            "temporal_semantics": api_cfg.get("temporal_semantics", {}),
        }

    merged = _merge_config_services(services, cfg)
    return _attach_service_contracts(merged, skill_service_ids)


def _merge_config_services(services: dict, cfg: dict) -> dict:
    """Merge runtime overrides without discarding skill request metadata.

    The configuration page only persists editable fields such as URL, label,
    and triggers. Replacing the complete skill entry with that partial object
    would remove request_map and make patient-scoped calls send only page/rows.
    """
    configured = cfg.get("services", {})
    if not isinstance(configured, dict):
        return services

    structural_keys = {
        "field_labels",
        "keep_fields",
        "merge",
        "method",
        "rec_prefix",
        "request_map",
        "request_wrapper",
        "returns",
        "semantic",
        "temporal_semantics",
    }

    for sid, svc in configured.items():
        if isinstance(sid, str) and isinstance(svc, dict) and svc.get("url"):
            skill_service = services.get(sid)
            if not isinstance(skill_service, dict):
                services[sid] = dict(svc)
                continue

            merged = dict(skill_service)
            for key, value in svc.items():
                if key == "_contract":
                    continue
                # Partial configuration objects must not erase non-empty
                # request/response metadata loaded from SKILL.md.
                if key in structural_keys and not value and skill_service.get(key):
                    continue
                merged[key] = value
            services[sid] = merged
    return services


def match_services(condition: str, services: dict = None, model: str = None) -> List[dict]:
    """Match query to external services. LLM semantic primary, keyword fallback."""
    if services is None:
        services = load_services()

    base_url = services.get("base_url", "").rstrip("/")

    # Filter valid services
    valid = {}
    for sid, svc in services.items():
        if sid == "base_url" or not isinstance(svc, dict):
            continue
        if not svc.get("url"):
            continue
        valid[sid] = svc

    if not valid:
        return []

    def _build(sid, svc):
        url = svc["url"]
        if base_url and not url.startswith("http"):
            url = f"{base_url}/{url.lstrip('/')}"
        return {**svc, "url": url, "id": sid}

    # ── Primary: LLM semantic matching ──
    llm_by_id = {}
    if model:
        for svc in (_match_services_llm(condition, valid, base_url, model) or []):
            llm_by_id[svc.get("id", "")] = svc

    # ── Keyword substring match (always runs, supplements LLM) ──
    kw_by_id = {}
    for sid, svc in valid.items():
        for t in svc.get("triggers", []):
            if t in condition:
                kw_by_id[sid] = svc
                break

    # Merge: keyword + LLM, deduplicated
    merged = {**kw_by_id, **llm_by_id}  # LLM priority for same key
    if merged:
        return [_build(sid, svc) for sid, svc in merged.items()]

    return []


def _extract_ids(parsed) -> list:
    """Normalize LLM output to list of service ID strings."""
    if not parsed:
        return []
    if isinstance(parsed, str):
        return [parsed] if parsed.strip() else []
    if isinstance(parsed, list):
        ids = []
        for item in parsed:
            if isinstance(item, str):
                ids.append(item)
            elif isinstance(item, dict):
                # {"service": "diagnosis-query"} or {"id": "diagnosis-query"}
                ids.append(item.get("service", item.get("id", item.get("name", ""))))
        return [i for i in ids if i and isinstance(i, str)]
    if isinstance(parsed, dict):
        svcs = parsed.get("services", [])
        return _extract_ids(svcs)
    return []


def _match_services_llm(condition: str, services: dict, base_url: str, model: str) -> List[dict]:
    """LLM semantic match: determine which external services are relevant."""
    menu_lines = []
    for sid, svc in services.items():
        desc = svc.get("description", sid)
        ret = svc.get("returns", "")
        triggers = ", ".join(svc.get("triggers", [])[:10])
        menu_lines.append(f"  [{sid}] {desc} | 返回: {ret} | 触发词示例: {triggers}")

    from microharness.ollama.model_profile import get_profile
    from microharness.ollama.prompt_adapter import build_service_router_prompt
    _sr_profile = get_profile(model)
    prompt = build_service_router_prompt(_sr_profile, condition, menu_lines)

    try:
        from microharness.ollama import OllamaClient
        from microharness.medical.query_router import parse_llm_json
        c = OllamaClient(model=model, timeout=120,
                        format_json=(_sr_profile.json_mode == "format_json"))
        resp = c.chat([{"role": "user", "content": prompt}], temperature=0.1)
        result = parse_llm_json(resp, context=f"服务路由:{condition[:30]}")
        service_ids = _extract_ids(result)
        # Fallback: LLM returned unquoted array like [diagnosis-query]
        if not service_ids:
            import re as _svcre
            arr_match = _svcre.search(r'\[([^\]]+)\]', resp)
            if arr_match:
                service_ids = [s.strip().strip('"').strip("'") for s in arr_match.group(1).split(',') if s.strip()]
        print(f"[LLM服务路由] {condition} → {service_ids}", flush=True)
    except Exception as e:
        print(f"[LLM服务路由] 失败: {e}", flush=True)
        return []

    matched = []
    for sid in service_ids:
        svc = services.get(sid)
        if svc:
            url = svc["url"]
            if base_url and not url.startswith("http"):
                url = f"{base_url}/{url.lstrip('/')}"
            matched.append({**svc, "url": url, "id": sid})
    return matched
