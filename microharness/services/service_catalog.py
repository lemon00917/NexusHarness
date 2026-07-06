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
from typing import Dict, List, Optional

_SKILLS_DIR = Path(__file__).parent.parent.parent / "skills"
_CONFIG_PATH = Path(__file__).parent.parent.parent / "configs" / "external_services.json"

# Shared base URL for external APIs
_BASE_URL = "http://43.143.68.242:9090/emviewdoctor/hdc/"


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
    try:
        if _CONFIG_PATH.exists():
            cfg = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
            url = cfg.get("base_url", "").strip()
            if url:
                return url
    except Exception:
        pass
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

    if not _SKILLS_DIR.exists():
        return services

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
