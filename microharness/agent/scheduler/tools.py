"""
Action Registry
===============
Maps action names to handler functions called by the ExecutionEngine.

Handlers: (params: dict, state: dict, ctx: ExecutionContext) → result dict
Result: {"ok": True, "data": [...], "matched": bool, "reason": ""} or {"ok": False, "error": ""}
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional
import re as _re
from datetime import datetime as _dt


@dataclass
class ExecutionContext:
    """Shared dependencies passed to all action handlers."""
    condition: str
    register_no: str = ""
    visit_no: str = ""
    global_patient_id: str = ""
    global_visit_id: str = ""
    router_model: str = "qwen2.5:3b"
    judge_model: str = "deepseek-r1:1.5b"


ActionHandler = Callable[[dict, dict, ExecutionContext], dict]
_registry: Dict[str, ActionHandler] = {}


def register(name: str):
    def decorator(fn: ActionHandler) -> ActionHandler:
        _registry[name] = fn
        return fn
    return decorator


def get_handler(name: str) -> Optional[ActionHandler]:
    return _registry.get(name)


def list_actions() -> list:
    return list(_registry.keys())


# ═══════════════════════════════════════════════════════════════
# Action Handlers
# ═══════════════════════════════════════════════════════════════

@register("merge_results")
def handle_merge_results(params: dict, state: dict, ctx: ExecutionContext) -> dict:
    """Merge multiple API result sets into one for unified processing."""
    sources = params.get("sources", [])
    merged_data = []
    for src_var in sources:
        src = state.get(src_var, {})
        if isinstance(src, dict) and src.get("ok"):
            merged_data.extend(src.get("data", []))
    print(f"    [action:merge_results] {len(sources)} sources → {len(merged_data)} total records", flush=True)
    return {"ok": True, "data": merged_data, "matched": len(merged_data) > 0,
            "reason": f"合并{len(sources)}个数据源,共{len(merged_data)}条"}


@register("query_db")
def handle_query_db(params: dict, state: dict, ctx: ExecutionContext) -> dict:
    """Query the database for documents/sections matching a condition.

    params: {condition: str, documents: [str], sections: [str]}
    """
    from web.app import _query_db
    from microharness.medical.query_router import QueryRouter
    import sys as _sys

    condition = params.get("condition", ctx.condition)
    documents = params.get("documents", [])
    sections = params.get("sections", [])
    _log = lambda m: print(f"    [action:query_db] {m}", flush=True)

    if not documents:
        # Try routing to find documents
        try:
            router = QueryRouter(model=ctx.router_model)
            route = router.route(condition)
            documents = route.get("target_medical_doc", [])
            sections = route.get("target_sections", [])
        except Exception:
            pass

    if not documents:
        return {"ok": False, "error": "无法路由到任何文档"}

    sq_route = {
        "targets": {d: list(sections) if sections else [] for d in documents},
        "target_medical_doc": documents,
        "target_sections": sections,
    }

    results = _query_db(sq_route, ctx.register_no, ctx.visit_no,
                        ctx.global_patient_id, ctx.global_visit_id, log_fn=_log)
    if not results and documents:
        try:
            from microharness.database.field_mapper import DOC_FIELDS
            expanded_targets = {
                doc: list((DOC_FIELDS.get(doc) or {}).keys())
                for doc in documents
                if DOC_FIELDS.get(doc)
            }
            if expanded_targets:
                _log("指定章节无结果 → 扩展到文档全部已映射章节重查")
                expanded_route = {
                    "targets": expanded_targets,
                    "target_medical_doc": list(expanded_targets.keys()),
                    "target_sections": sorted({s for secs in expanded_targets.values() for s in secs}),
                }
                results = _query_db(
                    expanded_route,
                    ctx.register_no,
                    ctx.visit_no,
                    ctx.global_patient_id,
                    ctx.global_visit_id,
                    log_fn=_log,
                )
        except Exception:
            pass
    return {"ok": True, "data": results, "matched": len(results) > 0,
            "reason": f"DB查询返回{len(results)}条记录"}


@register("call_service")
def handle_call_service(params: dict, state: dict, ctx: ExecutionContext) -> dict:
    """Call an external HTTP API service.

    params: {service: str, keyword: str}
      service: "diagnosis-query" | "drug-interaction" | "encounter-info"
    """
    from microharness.services.service_catalog import load_services
    from microharness.services.http_client import call_service_as_binding

    service_id = params.get("service", "")
    keyword = params.get("keyword", ctx.condition)

    services = load_services()
    base_url = services.get("base_url", "").rstrip("/")
    svc = services.get(service_id, {})
    if not svc or not svc.get("url"):
        return {"ok": False, "error": f"服务'{service_id}'不可用"}

    # Prepend base_url if the service URL is relative
    url = svc["url"]
    if base_url and not url.startswith("http"):
        url = f"{base_url}/{url.lstrip('/')}"
    svc = {**svc, "url": url}

    try:
        results = call_service_as_binding(
            svc, {"condition": keyword},
            register_no=ctx.register_no,
            global_patient_id=ctx.global_patient_id,
            visit_no=ctx.visit_no,
            global_visit_id=ctx.global_visit_id,
        )
        data = results or []
        print(f"    [action:call_service] {service_id} keyword='{keyword}' → {len(data)}条", flush=True)
        return {"ok": True, "data": data, "matched": len(data) > 0,
                "reason": f"服务{service_id}返回{len(data)}条"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@register("llm_judge")
def handle_llm_judge(params: dict, state: dict, ctx: ExecutionContext) -> dict:
    """LLM judgment: does the data match the condition?

    params: {condition: str, data_var: str (state key containing data)}
    """
    from microharness.ollama import OllamaClient
    from microharness.ollama.model_profile import get_profile
    from microharness.ollama.prompt_adapter import build_judge_prompt

    condition = params.get("condition", ctx.condition)
    data_var = params.get("data_var", "")
    data = state.get(data_var, {}).get("data", []) if data_var else []

    # Collect context from previous steps for LLM to write a good reason
    _med = state.get("medical_judgment", {})
    _med_reason = _med.get("reason", "")[:200] if isinstance(_med, dict) else ""
    _tf = state.get("filtered_data", {})

    # concept_filter is the deterministic guardrail: it already checked the
    # service-declared match fields (drug name, diagnosis name, etc.) inside the
    # temporal window. Do not let a small-model JSON/format failure override a
    # confirmed structured hit.
    if data_var == "filtered_data" and isinstance(_tf, dict) and _tf.get("matched") and data:
        return {
            "ok": True,
            "data": data,
            "matched": True,
            "reason": _tf.get("reason") or "已找到匹配的结构化医疗记录",
        }

    # Format field data for LLM. Handles two formats:
    # - call_service output: [{bindings: [{html_field, value}, ...]}, ...]
    # - temporal_filter output: [{html_field, value, _diff_hours}, ...]
    #
    # When data comes from temporal_filter, the matched items only contain
    # the date field that triggered the match. We cross-reference api_results
    # to pull the full record bindings (drug name, dosage, etc.) so the LLM
    # can actually judge whether the drug matches the condition.
    _data = data or []
    _api_results = state.get("api_results", {}).get("data", []) if state.get("api_results") else []

    # Extract matched record prefixes from temporal_filter output
    _matched_prefixes = set()
    for item in _data:
        if isinstance(item, dict):
            field = item.get("html_field", "")
            m = _re.match(r'(\[[^]]+\])', field)
            if m:
                _matched_prefixes.add(m.group(1))

    field_lines = []
    if _matched_prefixes and _api_results:
        # Cross-reference: pull matched record bindings from API results.
        # Read SKILL.md [匹配字段] to know which fields are key identifiers
        # (e.g., orderName for drug-interaction, diagnoseName for diagnosis-query).
        # Only include these key fields so the 3B model focuses on what matters.
        # Read SKILL.md metadata: [匹配字段] + [时间字段] + [分类字段]
        # Judge needs name + date + type to make accurate decisions
        _key_field_names = set()
        try:
            from microharness.services.service_catalog import load_services as _load_svc3
            _all_svcs = _load_svc3()
            for _svc_id in (_all_svcs or {}):
                _returns = _all_svcs[_svc_id].get("returns", "")
                for _tag in (r'\[匹配字段\]', r'\[时间字段\]', r'\[分类字段\]'):
                    for _m in _re.finditer(_tag + r'[^)]*\(([^)]+)\)', _returns):
                        # Extract individual field names: "orderDate+orderTime" → ["orderDate", "orderTime"]
                        _fields_str = _m.group(1).strip()
                        for _f in _re.split(r'[,+、]', _fields_str):
                            _f = _re.sub(r'[一-鿿]+$', '', _f.strip())  # strip trailing CJK
                            if _f:
                                _key_field_names.add(_f)
                    for _m in _re.finditer(_tag + r'\s*([^\s\-\(\n,+/]+)', _returns):
                        _key_field_names.add(_m.group(1).strip())
        except Exception:
            pass
        if not _key_field_names:
            _key_field_names = None
        for api_item in _api_results:
            if isinstance(api_item, dict):
                for b in api_item.get("bindings", []):
                    field = b.get("html_field", "")
                    m = _re.match(r'(\[[^]]+\])', field)
                    if m and m.group(1) in _matched_prefixes:
                        label = field
                        val = str(b.get("value", ""))[:300]
                        if label and val:
                            if _key_field_names is None or any(kf in label for kf in _key_field_names):
                                field_lines.append(f"{label}: {val}")
    else:
        # Fallback: iterate items directly (handles both bindings wrapper and flat)
        for item in _data[:20]:
            if isinstance(item, dict):
                if "bindings" in item:
                    for b in item["bindings"]:
                        label = b.get("html_field", "")
                        val = str(b.get("value", ""))[:500]
                        if label and val:
                            field_lines.append(f"{label}: {val}")
                elif "html_field" in item:
                    label = item.get("html_field", "")
                    val = str(item.get("value", ""))[:500]
                    if label and val:
                        diff_h = item.get("_diff_hours")
                        if diff_h is not None:
                            field_lines.append(f"{label}（术后{diff_h:.0f}h）: {val}")
                        else:
                            field_lines.append(f"{label}: {val}")

    if not field_lines:
        # Let LLM write a natural explanation for the negative result
        _tf_reason = _tf.get("reason", "") if isinstance(_tf, dict) else ""
        _neg_prompt = f"""患者筛选结果：不匹配。

原始条件：{condition}
医疗判断：{_med_reason}
时间筛选：{_tf_reason}

用自然语言解释为什么该患者不符合条件。30字以内。只输出解释文本。"""
        try:
            profile = get_profile(ctx.judge_model)
            client = OllamaClient(model=ctx.judge_model, timeout=30,
                                num_predict=80, format_json=False)
            _reason = client.chat([{"role": "user", "content": _neg_prompt}], temperature=0.1)
            _reason = _reason.strip()[:120]
        except Exception:
            _reason = f"该患者不符合条件"
        return {"ok": True, "data": [], "matched": False, "reason": _reason}

    judge_summary = "\n".join(field_lines[:30])

    print(f"    [action:llm_judge] field_lines={len(field_lines)}条, prefix_matches={len(_matched_prefixes)}个",
          flush=True)
    if field_lines:
        # Show first 3 fields as evidence
        for fl in field_lines[:3]:
            print(f"      {fl[:120]}", flush=True)

    profile = get_profile(ctx.judge_model)
    _hints = f"医疗判断结果：{_med_reason}\n时间筛选结果：{_tf.get('reason', '')}"
    prompt = build_judge_prompt(profile, condition, "", judge_summary, _hints)

    try:
        client = OllamaClient(model=ctx.judge_model, timeout=120,
                            num_predict=profile.num_predict,
                            format_json=(profile.json_mode == "format_json"))
        resp = client.chat([{"role": "user", "content": prompt}], temperature=0.1)
        from microharness.medical.query_router import parse_llm_json
        jd = parse_llm_json(resp, context=f"scheduler_judge:{condition[:30]}")
        matched = jd.get("matched", False)
        reason = jd.get("reason", "")
        if not reason and jd.get("reasoning"):
            reason = str(jd["reasoning"])[:200]
        return {"ok": True, "data": data, "matched": matched,
                "reason": reason or f"LLM判断{'匹配' if matched else '不匹配'}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _find_dates_in_text(val: str) -> list:
    """Extract date strings from text. Handles both datetime and date-only formats.
    Date-only values get ' 00:00:00' appended for uniform parsing.
    """
    try:
        from microharness.medical.time_window import parse_datetime_values
        parsed = parse_datetime_values(val)
        if parsed:
            return [dt.strftime("%Y-%m-%d %H:%M:%S") for dt in parsed]
    except Exception:
        pass
    matches = _re.findall(r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}', val)
    if not matches:
        date_only = _re.findall(r'\d{4}-\d{2}-\d{2}', val)
        for dm in date_only:
            parts = dm.split('-')
            if 1900 <= int(parts[0]) <= 2100 and 1 <= int(parts[1]) <= 12:
                matches.append(dm + " 00:00:00")
    return matches


def _record_prefix(field: str) -> str:
    m = _re.match(r'(\[[^]]+\])', field or "")
    return m.group(1) if m else ""


def _char_overlap_match(keyword: str, text: str) -> bool:
    """Loose but bounded CJK containment check.

    Two-character concepts require both chars. Longer concepts may miss one
    char to tolerate variants such as abbreviations, but not broad concepts like
    "感染" matching "感冒".
    """
    if not keyword or not text:
        return False
    if keyword in text:
        return True
    chars = list(keyword)
    found = sum(1 for c in chars if c in text)
    if len(chars) <= 2:
        return found == len(chars)
    return found >= len(chars) - 1


def _extract_target_keyword(target_text: str) -> str:
    """Strip query verbs/particles and keep the clinical concept to match."""
    text = (target_text or "").strip()
    if not text:
        return ""
    num_pat = r'(?:\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百千万亿]+)'
    unit_pat = r'(?:天|日|小时|分钟|周|月|个月)'
    text = _re.sub(
        rf'(^.+?\s*(?:前|后)\s*(?:当天|当日)|'
        rf'^.+?\s*(?:前|后)\s*{num_pat}\s*{unit_pat}(?:内|前|后)?|'
        rf'^.+?\s*{num_pat}\s*{unit_pat}\s*(?:前|后|内)|'
        rf'(?:当天|当日)|'
        rf'住院期间|住院期内|入院前|入院后|出院前|出院后|术前|术后|手术前|手术后|'
        rf'第?{num_pat}\s*{unit_pat}(?:内|前|后)?)',
        '',
        text,
    )
    text = _re.sub(
        r'(指标|检验|化验|项目|结果|数值|水平|计数值|偏高|偏低|升高|降低|增高|减少|异常)',
        '',
        text,
    )
    text = _re.sub(
        r'\s*(大于|小于|高于|低于|不超过|不低于|等于|>=|<=|>|<|=|≥|≤|＞|＜)\s*'
        r'[\d.]+\s*(?:[×x*]\s*10\S*)?(?:\S*/\S*)?',
        '',
        text,
    )
    # Generic medical-query verbs, not concrete drug/disease names.
    text = _re.sub(
        r'^(开了|开具了|开具|服用了|服用过|服用|吃了|使用了|使用过|使用|用过|'
        r'注射过|注射了|注射|诊断为|诊断成|诊断|确诊为|确诊|患有|存在|有)',
        '',
        text,
    )
    text = _re.sub(r'(的患者|的病人|患者|病人|病例|的人)$', '', text)
    text = _re.sub(r'^[了为过\s]+', '', text)
    text = _re.sub(r'[，。；;,.、\s]+', '', text)
    return text


def _service_match_fields(service_ids: list) -> set:
    """Read [匹配字段] declarations from SKILL.md metadata."""
    fields = set()
    try:
        from microharness.services.service_catalog import load_services
        services = load_services()
        for sid in service_ids or []:
            svc = services.get(sid, {})
            returns = svc.get("returns", "") if isinstance(svc, dict) else ""
            for m in _re.finditer(r'\[匹配字段\][^\n]*\(([^)]+)\)', returns):
                raw = m.group(1)
                for f in _re.split(r'[,+、/]', raw):
                    f = f.strip()
                    if f:
                        fields.add(f)
    except Exception:
        pass
    return fields


@register("concept_filter")
def handle_concept_filter(params: dict, state: dict, ctx: ExecutionContext) -> dict:
    """Filter time-matched records by service-declared concept fields.

    This is the deterministic guardrail before LLM judgment. It prevents cases
    where the time window is correct but no matching drug/diagnosis exists.
    """
    source_var = params.get("source_var", "")
    api_var = params.get("api_var", "api_results")
    target_text = params.get("target_text", "") or params.get("condition", ctx.condition)
    service_ids = params.get("services", []) or []

    source = state.get(source_var, {})
    source_data = source.get("data", []) if isinstance(source, dict) else []
    if not source_data:
        reason = source.get("reason", "无时间匹配记录") if isinstance(source, dict) else "无时间匹配记录"
        return {**source, "ok": True, "data": [], "matched": False, "reason": reason}

    keyword = _extract_target_keyword(target_text)
    if len(keyword) < 2:
        return {**source, "ok": True, "keyword": keyword}

    match_fields = _service_match_fields(service_ids)
    api_source = state.get(api_var, {})
    api_data = api_source.get("data", []) if isinstance(api_source, dict) else []

    prefixes = {_record_prefix(item.get("html_field", "")) for item in source_data if isinstance(item, dict)}
    prefixes.discard("")

    candidate_bindings = []
    for api_item in api_data:
        if not isinstance(api_item, dict):
            continue
        for b in api_item.get("bindings", []):
            label = b.get("html_field", "")
            if prefixes and _record_prefix(label) not in prefixes:
                continue
            candidate_bindings.append(b)

    try:
        from microharness.medical.lab_rules import judge_lab_condition
        lab_judge = judge_lab_condition(target_text, candidate_bindings)
    except Exception:
        lab_judge = {"applicable": False}
    if lab_judge.get("applicable"):
        hit_prefixes = set(lab_judge.get("matched_prefixes") or [])
        time_reason = source.get("reason", "") if isinstance(source, dict) else ""
        if lab_judge.get("matched"):
            filtered = [
                item for item in source_data
                if isinstance(item, dict)
                and (not hit_prefixes or _record_prefix(item.get("html_field", "")) in hit_prefixes)
            ]
            if not filtered:
                filtered = source_data
            return {
                **source,
                "ok": True,
                "data": filtered,
                "matched": True,
                "reason": f"{time_reason}，{lab_judge.get('reason', '检验记录符合条件')}",
                "keyword": lab_judge.get("keyword", keyword),
                "matched_records": lab_judge.get("fields", "")[:2000],
            }
        return {
            **source,
            "ok": True,
            "data": [],
            "matched": False,
            "reason": f"{time_reason}，{lab_judge.get('reason', '检验记录不符合条件')}",
            "keyword": lab_judge.get("keyword", keyword),
            "matched_records": lab_judge.get("fields", "")[:2000],
        }

    matched_prefixes = set()
    matched_lines = []

    def field_is_match_field(binding: dict) -> bool:
        if not match_fields:
            return True
        eng = binding.get("eng_field", "")
        label = binding.get("html_field", "")
        return eng in match_fields or any(f in label for f in match_fields)

    for api_item in api_data:
        if not isinstance(api_item, dict):
            continue
        for b in api_item.get("bindings", []):
            label = b.get("html_field", "")
            prefix = _record_prefix(label)
            if prefixes and prefix not in prefixes:
                continue
            if not field_is_match_field(b):
                continue
            val = str(b.get("value", ""))
            if _char_overlap_match(keyword, val):
                if prefix:
                    matched_prefixes.add(prefix)
                matched_lines.append(f"{label}: {val[:120]}")

    degraded_field_scan = False
    if not matched_lines and match_fields:
        degraded_field_scan = True
        for api_item in api_data:
            if not isinstance(api_item, dict):
                continue
            for b in api_item.get("bindings", []):
                label = b.get("html_field", "")
                prefix = _record_prefix(label)
                if prefixes and prefix not in prefixes:
                    continue
                val = str(b.get("value", ""))
                if _char_overlap_match(keyword, val):
                    if prefix:
                        matched_prefixes.add(prefix)
                    matched_lines.append(f"{label}: {val[:120]}")

    if prefixes:
        filtered = [
            item for item in source_data
            if isinstance(item, dict) and _record_prefix(item.get("html_field", "")) in matched_prefixes
        ]
    else:
        filtered = [
            item for item in source_data
            if isinstance(item, dict) and _char_overlap_match(keyword, str(item.get("value", "")))
        ]

    time_reason = source.get("reason", "") if isinstance(source, dict) else ""
    if filtered:
        degrade_note = "（字段映射降级扫描）" if degraded_field_scan else ""
        reason = f"{time_reason}，其中{len(filtered)}条匹配'{keyword}'{degrade_note}"
        return {
            **source,
            "ok": True,
            "data": filtered,
            "matched": True,
            "reason": reason,
            "keyword": keyword,
            "matched_records": "\n".join(matched_lines[:20]),
        }

    reason = f"{time_reason}，但匹配字段中未找到'{keyword}'"
    return {
        **source,
        "ok": True,
        "data": [],
        "matched": False,
        "reason": reason,
        "keyword": keyword,
        "matched_records": "",
    }


@register("extract_date")
def handle_extract_date(params: dict, state: dict, ctx: ExecutionContext) -> dict:
    """Extract date values from binding data.

    params: {source_var: str, field: str}
    Returns first matching date found.
    """
    source_var = params.get("source_var", "")
    target_field = params.get("field", "")
    source = state.get(source_var, {})

    if not source:
        return {"ok": False, "error": f"源变量'{source_var}'为空"}

    data = source.get("data", [source] if not isinstance(source, dict) else [])
    if isinstance(data, dict):
        data = [data]

    dates_found = []
    for item in data:
        if isinstance(item, dict):
            for b in item.get("bindings", []):
                label = b.get("html_field", "")
                val = str(b.get("value", ""))
                if target_field and target_field not in label:
                    continue
                for m in _find_dates_in_text(val):
                    dates_found.append({"field": label, "date": m, "raw": val})

    if not dates_found:
        # Try without field filter
        for item in data:
            if isinstance(item, dict):
                for b in item.get("bindings", []):
                    val = str(b.get("value", ""))
                    for m in _find_dates_in_text(val):
                        dates_found.append({"field": b.get("html_field", ""), "date": m, "raw": val})

    if dates_found:
        print(f"    [action:extract_date] 从'{source_var}'提取到{len(dates_found)}个日期: {dates_found[:3]}", flush=True)
        return {"ok": True, "data": dates_found, "dates": dates_found,
                "matched": True, "reason": f"提取到{len(dates_found)}个日期"}

    return {"ok": False, "error": f"未在'{source_var}'中找到日期字段"}


@register("temporal_filter")
def handle_temporal_filter(params: dict, state: dict, ctx: ExecutionContext) -> dict:
    """Filter records by time relation to a reference date.

    params: {reference_var: str, target_var: str, relation: str,
             value: float, unit: str}
      relation: "within" | "before" | "after"
      unit: "hours" | "days"
    """
    ref_var = params.get("reference_var", "")
    target_var = params.get("target_var", "")
    relation = params.get("relation", "within")
    value = float(params.get("value", 24))
    unit = params.get("unit", "hours")
    lower_hours = params.get("lower_hours", None)
    upper_hours = params.get("upper_hours", None)
    calendar_day = bool(params.get("calendar_day", False))

    ref_data = state.get(ref_var, {})
    target_data = state.get(target_var, {})

    if not ref_data or not ref_data.get("dates"):
        return {"ok": False, "error": f"参考日期'{ref_var}'为空"}

    if not target_data:
        return {"ok": False, "error": f"目标数据'{target_var}'为空"}

    # Get reference date. Surgery date may be a start/end range; use start for
    # pre-op windows and end for post-op windows.
    ref_dates = ref_data["dates"]
    condition_text = ctx.condition or ""
    if len(ref_dates) > 1 and (
        relation == "after" or (relation == "within" and _re.search(r"(术后|手术后)", condition_text))
    ):
        ref_date_str = ref_dates[-1]["date"]
    else:
        ref_date_str = ref_dates[0]["date"]
    try:
        ref_dt = _dt.fromisoformat(ref_date_str.replace(' ', 'T'))
    except ValueError:
        return {"ok": False, "error": f"无法解析参考日期'{ref_date_str}'"}

    # Convert value to hours for consistent comparison
    value_hours = value if unit == "hours" else value * 24

    # Filter target data by time relation
    target_list = target_data.get("data", [])
    matched = []
    candidates = []
    for item in target_list:
        if isinstance(item, dict):
            for b in item.get("bindings", []):
                val = str(b.get("value", ""))
                date_matches = _find_dates_in_text(val)
                for dm in date_matches:
                    try:
                        item_dt = _dt.fromisoformat(dm.replace(' ', 'T'))
                        diff = item_dt - ref_dt
                        total_h = diff.total_seconds() / 3600.0
                        cand = {**b, "_diff_hours": total_h, "_date": dm}
                        candidates.append(cand)

                        if calendar_day:
                            same_date = item_dt.date() == ref_dt.date()
                            if relation == "before":
                                in_window = same_date and item_dt <= ref_dt
                            elif relation == "after":
                                in_window = same_date and item_dt >= ref_dt
                            else:
                                in_window = same_date
                            if in_window:
                                matched.append(cand)
                        elif lower_hours is not None and upper_hours is not None:
                            lo = float(lower_hours)
                            hi = float(upper_hours)
                            if lo == 0:
                                in_window = 0 < total_h <= hi
                            elif hi == 0:
                                in_window = lo <= total_h < 0
                            else:
                                in_window = lo <= total_h < hi
                            if in_window:
                                matched.append(cand)
                        elif relation == "within":
                            if 0 < total_h <= value_hours:
                                matched.append(cand)
                        elif relation == "before":
                            if -value_hours <= total_h < 0:
                                matched.append(cand)
                        elif relation == "after":
                            if total_h >= value_hours:
                                matched.append(cand)
                    except ValueError:
                        pass

    # Build user-friendly reason
    _rel_desc = {"within": "内", "before": "前", "after": "后"}
    _unit_desc = {"hours": "小时", "days": "天", "weeks": "周", "minutes": "分钟"}
    _rd = _rel_desc.get(relation, relation)
    _ud = _unit_desc.get(unit, unit)
    # Build natural-language reason with actual dates
    _date_display = ref_date_str[:10] if ref_date_str else "?"
    if calendar_day:
        if relation == "before":
            _time_desc = f"{_date_display}当天且不晚于参考时间"
        elif relation == "after":
            _time_desc = f"{_date_display}当天且不早于参考时间"
        else:
            _time_desc = f"{_date_display}当天"
    elif relation == "after":
        _time_desc = f"{_date_display}之后{int(value)}{_ud}"
    elif relation == "before":
        _time_desc = f"{_date_display}之前{int(value)}{_ud}"
    else:
        _time_desc = f"{_date_display}之后{int(value)}{_ud}内"

    if len(matched) > 0:
        _reason = f"{_time_desc}找到{len(matched)}条匹配记录"
    else:
        _reason = f"{_time_desc}无匹配记录"

    print(f"    [action:temporal_filter] ref={ref_date_str} {relation} {value}{unit} → {len(matched)}条",
          flush=True)
    # Keep nearest raw candidates for negative evidence as well. This lets the
    # API explain what was found outside the requested time window.
    candidates_sorted = sorted(
        candidates,
        key=lambda x: abs(float(x.get("_diff_hours", 10**9)))
    )
    return {"ok": True, "data": matched, "matched": len(matched) > 0,
            "reason": _reason, "anchor_date": ref_date_str,
            "time_desc": _time_desc,
            "candidate_data": candidates_sorted[:20],
            "candidate_count": len(candidates)}


@register("boolean_combine")
def handle_boolean_combine(params: dict, state: dict, ctx: ExecutionContext) -> dict:
    """Combine multiple sub-results with AND/OR logic.

    params: {vars: [str], logic: "and"|"or"}
    """
    var_names = params.get("vars", [])
    logic = params.get("logic", "and")

    if not var_names:
        return {"ok": False, "error": "未指定要组合的变量"}

    step_results = {}
    for vn in var_names:
        r = state.get(vn, {})
        step_results[vn] = {"matched": r.get("matched", False), "reason": r.get("reason", "")}

    matched_list = [v["matched"] for v in step_results.values()]
    matched = all(matched_list) if logic == "and" else any(matched_list)

    # Build natural-language reason
    if logic == "and":
        if matched:
            parts = []
            seen_reasons = set()
            for vn in var_names:
                r = step_results[vn]
                if r.get("reason"):
                    part = r["reason"][:120]
                    if part not in seen_reasons:
                        seen_reasons.add(part)
                        parts.append(part)
            reason = "该患者符合条件：" + "；".join(parts) if parts else "该患者符合全部条件"
        else:
            # Identify what failed and why, in plain language
            fail_msgs = []
            med_fail = not step_results.get("medical_judgment", {}).get("matched", True)
            time_fail = not step_results.get("filtered_data", {}).get("matched", True)

            if time_fail:
                _tf_reason = step_results.get("filtered_data", {}).get("reason", "")
                if med_fail:
                    reason = f"该患者不符合条件：{_tf_reason}"
                else:
                    reason = f"该患者存在相关医疗记录，但{_tf_reason}"
            elif med_fail:
                reason = "该患者不符合条件：病历中未找到相关医疗记录"
            else:
                failed = [v.get("reason", "") for v in step_results.values() if not v.get("matched")]
                failed = [r for r in failed if r]
                if failed:
                    reason = "该患者不符合条件：" + "；".join(failed[:3])
                else:
                    reason = "该患者不符合条件"
    else:
        if matched:
            hit_parts = []
            for vn, v in step_results.items():
                if v["matched"] and v.get("reason"):
                    hit_parts.append(v["reason"][:80])
            reason = "该患者满足任一条件：" + "；".join(hit_parts) if hit_parts else "该患者满足任一条件"
        else:
            reason = "该患者不满足任何条件"

    return {"ok": True, "matched": matched, "reason": reason}


@register("legacy_pipeline")
def handle_legacy_pipeline(params: dict, state: dict, ctx: ExecutionContext) -> dict:
    """Fallback: run the full existing pipeline.

    Uses params.condition if provided, otherwise ctx.condition.
    """
    from web.app import _run_medical_query
    condition = params.get("condition", ctx.condition)
    try:
        result = _run_medical_query(
            condition=condition,
            register_no=ctx.register_no,
            visit_no=ctx.visit_no,
            global_patient_id=ctx.global_patient_id,
            global_visit_id=ctx.global_visit_id,
            judge_model=ctx.judge_model,
            router_model=ctx.router_model,
            planner_model=None,  # no recursion
        )
        if result.get("results"):
            r = result["results"][0]
            # Preserve per_condition details from the existing pipeline
            return {"ok": True, "matched": r.get("matched", False),
                    "reason": r.get("reason", "legacy pipeline"),
                    "data": r.get("all_files", []),
                    "per_condition": r.get("per_condition", {}),
                    "all_files": r.get("all_files", [])}
        return {"ok": True, "matched": False, "reason": "legacy pipeline: 无结果"}
    except Exception as e:
        return {"ok": False, "error": f"legacy pipeline失败: {e}"}
