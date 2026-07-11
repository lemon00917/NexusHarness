"""
Query Understanding Module
==========================
Unified single-LLM-call query analysis that replaces 4 separate stages:
  1. analyze_query (structure: type/negated/conditions)
  2. router.route (document + section routing)
  3. _decompose_semantic (keyword + modifiers extraction)
  4. match_services (external skill/service matching)

Uses deepseek-r1:1.5b with format_json for reliable structured output.
Falls back to legacy analyze_query if LLM fails.
"""

import json
import os
import sys
from typing import Optional


def _understanding_debug_enabled() -> bool:
    return str(os.environ.get("MEDICAL_QUERY_DEBUG", "")).lower() in {"1", "true", "yes", "on"}


def _understanding_debug(message: str) -> None:
    if _understanding_debug_enabled():
        print(message, flush=True)


def understand_query(condition: str, model: str = "qwen2.5:3b",
                     timeout: int = 60, document_catalog: dict | None = None) -> dict:
    """Analyze a medical query in a single LLM call.

    Uses the provided model (default qwen2.5:3b for best Chinese instruction following).
    For models with format_json support (deepseek-r1), JSON output is guaranteed.
    For parse-mode models, robust JSON extraction is used.

    Returns a unified structure:
    {
        "type": "simple"|"compound"|"temporal",
        "negated": bool,          # negation detected ANYWHERE in condition
        "connector": "and"|"or"|null,
        "conditions": [
            {
                "text": "sub-condition text",
                "keyword": "core medical concept",
                "modifiers": ["modifier1"],   # status/negation words
                "is_numeric": bool,
                "target_docs": ["文档名"],     # routing: which documents
                "target_sections": ["章节名"],  # routing: which sections
                "target_skills": ["skill-id"], # routing: which external services
            }
        ],
        "source": "understand_query",
    }

    Falls back to legacy analyze_query + keyword routing if LLM fails.
    """
    # ═══════════════════════════════════════════════════════════
    # Build compact catalogs for the prompt
    # ═══════════════════════════════════════════════════════════
    doc_catalog = _build_compact_catalog(document_catalog)
    skills_menu = _build_skills_menu()

    # ═══════════════════════════════════════════════════════════
    # Call LLM with format_json for reliable output
    # ═══════════════════════════════════════════════════════════
    try:
        from microharness.ollama import OllamaClient
        from microharness.ollama.model_profile import get_profile
        from microharness.ollama.prompt_adapter import build_query_understanding_prompt
        from microharness.medical.query_router import parse_llm_json

        profile = get_profile(model)
        prompt = build_query_understanding_prompt(profile, condition, doc_catalog, skills_menu)

        # Always use format_json for understand_query — guarantees valid JSON output
        # regardless of model (deepseek-r1, qwen3.5:4b, qwen2.5:3b all support it)
        # Larger timeout for thinking models (qwen3.5:4b takes 20-30s)
        is_thinking = "qwen3" in model.lower() or "r1" in model.lower() or "think" in model.lower()
        client = OllamaClient(
            model=model, timeout=timeout if not is_thinking else max(timeout, 90),
            format_json=True,  # force JSON output for all models
            num_predict=4096 if is_thinking else 2048,
        )
        resp = client.chat([{"role": "user", "content": prompt}], temperature=0.0)

        result = parse_llm_json(resp, context=f"understand_query:{condition[:30]}")
        if not isinstance(result, dict) or not result.get("conditions"):
            print(f"[understand_query] LLM返回无效，走fallback", flush=True)
            _understanding_debug(f"[understand_query][debug] 原始响应: {resp[:500]}")
            return _fallback_understand(condition, model)

        # ═══════════════════════════════════════════════════════════
        # Validate and normalize the result
        # ═══════════════════════════════════════════════════════════
        result = _validate_and_normalize(result, condition, doc_catalog)

        result["source"] = "understand_query"
        _understanding_debug(f"[understand_query] {condition[:40]} → type={result['type']} "
              f"negated={result['negated']} conds={len(result['conditions'])}")
        for i, c in enumerate(result["conditions"], 1):
            _understanding_debug(f"  子条件{i}: kw={c.get('keyword','')} mod={c.get('modifiers',[])} "
                  f"docs={c.get('target_docs',[])} skills={c.get('target_skills',[])}")

        return result

    except Exception as e:
        print(f"[understand_query] 异常({e}) → fallback", flush=True)
        return _fallback_understand(condition, model)


def _build_compact_catalog(document_catalog: dict | None = None) -> dict:
    """Build compact DOCUMENT_CATALOG for prompt injection."""
    from microharness.medical.query_router import DOCUMENT_CATALOG

    source_catalog = document_catalog if document_catalog is not None else DOCUMENT_CATALOG
    compact = {}
    for doc_name, doc_info in source_catalog.items():
        sections = {}
        for sec in doc_info.get("sections", []):
            sec_name = sec.get("name", "")
            sec_purpose = sec.get("purpose", "")
            sections[sec_name] = sec_purpose
        purpose = doc_info.get("purpose", "")
        used_for = doc_info.get("used_for", [])
        compact[doc_name] = {"purpose": purpose, "used_for": used_for, "sections": sections}
    return compact


def _build_skills_menu() -> list:
    """Build skills menu for prompt injection."""
    try:
        from microharness.services.service_catalog import load_services
        services = load_services()
        menu = []
        for key, svc in services.items():
            if key == "base_url":
                continue
            if not isinstance(svc, dict):
                continue
            sid = svc.get("id", key)
            desc = svc.get("description", svc.get("desc", ""))
            label = svc.get("label", desc or sid)
            triggers = svc.get("triggers", [])
            returns = svc.get("returns", "")
            triggers_str = "、".join(triggers) if triggers else ""
            menu.append({
                "id": sid,
                "name": label,
                "desc": desc,
                "triggers": triggers_str,
                "returns": returns,
            })
        return menu
    except Exception as e:
        print(f"[understand_query] 加载skills失败: {e}", flush=True)
        return []


def _validate_and_normalize(result: dict, condition: str, doc_catalog: dict) -> dict:
    """Validate and normalize the LLM output.

    - Ensures required fields exist
    - Filters invalid section names against catalog
    - Ensures at least one condition
    """
    # Ensure required top-level fields
    result.setdefault("type", "simple")
    result.setdefault("negated", False)
    result.setdefault("connector", None)
    result.setdefault("conditions", [])

    # If no conditions, wrap the original condition
    if not result["conditions"]:
        result["conditions"] = [{
            "text": condition,
            "keyword": condition,
            "modifiers": [],
            "is_numeric": False,
            "target_docs": [],
            "target_sections": [],
            "target_skills": [],
        }]

    # Validate each condition
    valid_section_names = set()
    for doc_info in doc_catalog.values():
        valid_section_names.update(doc_info.get("sections", {}).keys())

    for cond in result["conditions"]:
        cond.setdefault("text", condition)
        cond.setdefault("keyword", cond["text"])
        cond.setdefault("entity", cond.get("keyword", cond["text"]))
        cond.setdefault("entity_type", "unknown")
        cond.setdefault("predicate", "unknown")
        cond.setdefault("modifiers", [])
        cond.setdefault("is_numeric", False)
        cond.setdefault("target_docs", [])
        cond.setdefault("target_sections", [])
        cond.setdefault("target_skills", [])

        # Normalize keyword: if LLM returned a list, join to string
        kw = cond["keyword"]
        if isinstance(kw, list):
            cond["keyword"] = kw[0] if kw else cond["text"]
        elif not isinstance(kw, str):
            cond["keyword"] = str(kw) if kw else cond["text"]

        for key in ("entity", "entity_type", "predicate"):
            if not isinstance(cond.get(key), str):
                cond[key] = str(cond.get(key) or "")
        if cond.get("entity") and (
            not cond.get("keyword")
            or str(cond.get("keyword")) == str(cond.get("text"))
            or str(cond.get("keyword")).startswith(("有", "患有", "存在", "诊断"))
        ):
            cond["keyword"] = cond["entity"]

        # Filter invalid document names. LLMs sometimes return placeholders like
        # "文档名"; those must not reach DB routing.
        if cond["target_docs"]:
            valid_docs = [d for d in cond["target_docs"] if d in doc_catalog]
            invalid_docs = [d for d in cond["target_docs"] if d not in doc_catalog]
            if invalid_docs:
                print(f"[understand_query] 过滤无效文档: {invalid_docs}", flush=True)
            cond["target_docs"] = valid_docs

        # Filter invalid section names
        if cond["target_sections"]:
            valid_secs = [s for s in cond["target_sections"] if s in valid_section_names]
            invalid = [s for s in cond["target_sections"] if s not in valid_section_names]
            if invalid:
                print(f"[understand_query] 过滤无效章节: {invalid}", flush=True)
            cond["target_sections"] = valid_secs

        # Ensure modifiers is a list
        if not isinstance(cond["modifiers"], list):
            cond["modifiers"] = []
        # Filter out numeric/generic modifiers
        import re
        cond["modifiers"] = [m for m in cond["modifiers"]
                            if isinstance(m, str) and len(m) >= 1
                            and m not in ("患者", "病人", "病例", "的")]

    return result


def _fallback_understand(condition: str, model: str) -> dict:
    """Fallback to legacy analyze_query + keyword routing when LLM fails.

    Uses the original analyze_query for structure, then QueryRouter for routing,
    and _decompose_semantic for keyword/modifiers. This preserves backward compat.
    """
    print(f"[understand_query] 使用fallback分析: {condition[:40]}", flush=True)

    from microharness.agent.query_analyzer import analyze_query
    analysis = analyze_query(condition, model=model)

    # Convert to unified format
    conditions = []
    for c in analysis.get("conditions", []):
        cond_text = c.get("text", condition)
        conditions.append({
            "text": cond_text,
            "keyword": cond_text,
            "modifiers": [],
            "is_numeric": False,
            "target_docs": [],      # Will be filled by check_one_condition fallback
            "target_sections": [],
            "target_skills": [],
        })

    return {
        "type": analysis.get("type", "simple"),
        "negated": analysis.get("negated", False),
        "connector": analysis.get("connector"),
        "conditions": conditions,
        "source": "fallback",
    }
