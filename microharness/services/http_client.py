"""
External Service HTTP Client
============================
Generic HTTP caller for external medical APIs.
"""

import json
import os
import re
import requests
from typing import Dict, Optional


DEFAULT_SERVICE_TIMEOUT_SECONDS = int(os.environ.get("EXTERNAL_SERVICE_TIMEOUT_SECONDS", "180"))


def _api_debug_enabled() -> bool:
    return str(os.environ.get("MEDICAL_QUERY_DEBUG", "")).lower() in {"1", "true", "yes", "on"}


def _clean_log_text(text: str, limit: int = 160) -> str:
    text = str(text or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _api_log(message: str, debug: bool = False) -> None:
    if debug and not _api_debug_enabled():
        return
    print(message, flush=True)


def _response_preview(value, limit: int = 800) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        text = repr(value)
    return _clean_log_text(text, limit=limit)


def _normalize_response_records(data, skill_id: str) -> list:
    """Extract record rows and make unsupported response shapes observable."""
    if not isinstance(data, list):
        _api_log(
            f"[外部API][解析异常] {skill_id} 根节点应为list，实际={type(data).__name__} "
            f"| 响应摘要={_response_preview(data)}"
        )
        return []
    if len(data) != 1 or not isinstance(data[0], dict):
        return data

    wrapper = data[0]
    if "data" not in wrapper:
        return data

    inner = wrapper.get("data")
    if isinstance(inner, list):
        _api_log(f"[外部API] {skill_id} 解析记录数={len(inner)} | 路径=data")
        return inner

    if isinstance(inner, dict):
        for key in ("records", "rows", "items", "list", "content", "result"):
            nested = inner.get(key)
            if isinstance(nested, list):
                _api_log(
                    f"[外部API] {skill_id} 解析记录数={len(nested)} "
                    f"| 路径=data.{key}"
                )
                return nested

        envelope_keys = {"success", "code", "message", "msg", "otherMsg", "total", "pages", "pageSize"}
        if inner and not (set(inner) & envelope_keys):
            _api_log(f"[外部API] {skill_id} 解析记录数=1 | 路径=data(单对象)")
            return [inner]

    outer_keys = list(wrapper.keys())
    inner_keys = list(inner.keys()) if isinstance(inner, dict) else []
    _api_log(
        f"[外部API][解析异常] {skill_id} 无法提取记录列表 "
        f"| 期望路径=data或data.records/rows/items/list/content/result "
        f"| 实际data类型={type(inner).__name__} "
        f"| 外层keys={outer_keys} | 内层keys={inner_keys} "
        f"| 响应摘要={_response_preview(wrapper)}"
    )
    return []


def _business_error(data) -> str:
    """Return a business-level error carried inside an HTTP 200 response."""
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
        return ""
    payload = data[0]
    message = next(
        (str(payload.get(key) or "").strip() for key in ("error", "message", "msg", "otherMsg") if payload.get(key)),
        "",
    )
    code = payload.get("code")
    try:
        numeric_code = int(str(code).strip()) if code is not None else None
    except (TypeError, ValueError):
        numeric_code = None
    if numeric_code is not None and numeric_code >= 400:
        return message or f"外部接口返回业务失败(code={numeric_code})"
    if payload.get("success") is False:
        return message or "外部接口返回业务失败"
    if "data" in payload and payload.get("data") is None and message:
        return message
    return ""


def call_service(url: str, method: str = "GET", params: dict = None,
                 timeout: int = DEFAULT_SERVICE_TIMEOUT_SECONDS, as_form: bool = False) -> dict:
    """
    Call an external service and return results in a uniform format.

    Args:
        url: API endpoint
        method: HTTP method (GET/POST)
        params: query parameters or body
        timeout: timeout seconds
        as_form: if True, send POST as URL-encoded form data instead of JSON

    Returns:
        {"ok": bool, "data": list, "error": str}
    """
    try:
        if method.upper() == "GET":
            resp = requests.get(url, params=params, timeout=timeout)
        elif as_form:
            resp = requests.post(url, data=params, timeout=timeout)
        else:
            resp = requests.post(url, json=params, timeout=timeout)

        if resp.status_code == 200:
            data = resp.json()
            # Normalize: always return a list of records
            if isinstance(data, dict):
                data = [data]
            elif not isinstance(data, list):
                data = [{"raw": str(data)}]
            return {"ok": True, "data": data}
        else:
            return {"ok": False, "data": [], "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}

    except requests.exceptions.Timeout:
        return {"ok": False, "data": [], "error": "请求超时"}
    except Exception as e:
        return {"ok": False, "data": [], "error": str(e)[:200]}


def _resolve_value(template: str, id_values: dict) -> str:
    """Resolve a value from template like '{{global_visit_id}}._bfc' → part before '_'."""
    # Strip {{ }} wrappers if present
    key = template.strip()
    if key.startswith("{{") and key.endswith("}}"):
        key = key[2:-2]
    if key.endswith("._bfc"):
        val = id_values.get(key[:-5], "")
        return val.split("_")[0] if "_" in val else val
    return id_values.get(key, template)


def _build_params(req_map: dict, id_values: dict) -> dict:
    """Recursively build params from request_map template."""
    result = {}
    for k, v in req_map.items():
        if isinstance(v, dict):
            result[k] = _build_params(v, id_values)
        else:
            result[k] = _resolve_value(v, id_values)
    return result


def _normalize_global_patient_id(register_no: str, global_patient_id: str, global_visit_id: str) -> str:
    """Repair common patient-id prefix typos using the encounter-id prefix.

    For this API family, hdcPatientId and hdcEncId share the business-field
    prefix before "_". If the supplied patient id has the right numeric suffix
    but a different prefix width (for example 0001_120 vs 00001_120), use the
    prefix from global_visit_id. This is schema-based, not query-specific.
    """
    gpid = str(global_patient_id or "").strip()
    gvid = str(global_visit_id or "").strip()
    reg = str(register_no or "").strip()
    if "_" not in gvid or not reg:
        return gpid
    prefix = gvid.split("_", 1)[0]
    try:
        reg_num = str(int(reg))
    except Exception:
        return gpid
    candidate = f"{prefix}_{reg_num}"
    if not gpid:
        return candidate
    if "_" not in gpid:
        return gpid
    gpid_prefix, gpid_suffix = gpid.split("_", 1)
    try:
        suffix_same = str(int(gpid_suffix)) == reg_num
    except Exception:
        suffix_same = False
    if suffix_same and gpid_prefix != prefix:
        return candidate
    return gpid


def call_service_as_binding(svc: dict, params: dict, register_no: str = "",
                             global_patient_id: str = "", visit_no: str = "",
                             global_visit_id: str = "") -> Optional[list]:
    """
    Call a service and convert results to binding-like format.
    Supports GET with query params and POST with JSON body.
    """
    normalized_global_patient_id = _normalize_global_patient_id(
        register_no, global_patient_id, global_visit_id
    )
    id_values = {
        "global_patient_id": normalized_global_patient_id,
        "global_visit_id": global_visit_id,
        "register_no": register_no,
        "visit_no": visit_no,
        "condition": params.get("condition", ""),
    }

    skill_id = svc.get("id", svc.get("name", "external"))
    skill_label = svc.get("label", svc.get("name", skill_id))
    req_map = svc.get("request_map", {})
    wrapper = svc.get("request_wrapper", "")
    method = svc.get("method", "POST").upper()
    url = svc["url"]
    service_timeout = int(svc.get("timeout", DEFAULT_SERVICE_TIMEOUT_SECONDS))

    if not isinstance(req_map, dict) or not req_map:
        config_fields = sorted(str(key) for key in svc.keys())
        _api_log(
            f"[外部API][配置异常] {skill_id} request_map为空 "
            f"| request_wrapper={wrapper or '(空)'} "
            f"| 服务配置字段={config_fields} "
            f"| 已阻止无患者范围的外部接口调用"
        )
        user_message = (
            f"{skill_label}服务缺少请求参数映射，未向外部接口发送请求，"
            "当前无法用该结构化数据源判断"
        )
        return [{
            "file": f"{skill_label} (配置异常)",
            "template": skill_label,
            "bindings": [
                {"html_field": "接口状态", "value": "配置异常", "xml_path": "external/service_status"},
                {"html_field": "说明", "value": user_message, "xml_path": "external/service_message"},
            ],
            "visit_no": visit_no or "",
            "keep_fields": svc.get("keep_fields"),
            "rec_prefix": svc.get("rec_prefix", "记录"),
            "field_labels": svc.get("field_labels", {}),
            "merge": svc.get("merge", []),
            "temporal_semantics": svc.get("temporal_semantics", {}),
            "semantic": svc.get("semantic", {}),
            "service_error": True,
            "service_id": skill_id,
            "error": user_message,
            "debug_error": "service request_map is empty",
        }]

    # Build request body
    business_params = _build_params(req_map, id_values)
    body = dict(business_params)
    if wrapper:
        body = {wrapper: json.dumps(body)}

    # GET: pass as query param
    if method == "GET":
        import urllib.parse
        if wrapper:
            url += "?" + urllib.parse.urlencode(body)
        else:
            url += "?" + urllib.parse.urlencode(body)
        url += "&page=1&rows=200"  # fetch all records, not just one page
        import urllib.parse
        _api_log(f"[外部API] {skill_id} GET {url.split('?')[0]} rows=200 timeout={service_timeout}s")
        _api_log(
            f"[外部API][完整入参] {skill_id} GET "
            f"| 业务参数={json.dumps(business_params, ensure_ascii=False, default=str)} "
            f"| 实际URL={urllib.parse.unquote(url)}"
        )
        _api_log(f"[外部API][debug] {skill_id} url={urllib.parse.unquote(url)[:500]}", debug=True)
        result = call_service(url, "GET", timeout=service_timeout)
    else:
        # POST: same params as GET, but in POST body (form-encoded)
        import urllib.parse

        body["page"] = 1
        body["rows"] = 200
        encoded_form_body = urllib.parse.urlencode(body)
        _api_log(f"[外部API] {skill_id} POST {url} rows=200 timeout={service_timeout}s")
        _api_log(
            f"[外部API][完整入参] {skill_id} POST "
            f"| 业务参数={json.dumps(business_params, ensure_ascii=False, default=str)} "
            f"| 实际提交={json.dumps(body, ensure_ascii=False, default=str)} "
            f"| Content-Type=application/x-www-form-urlencoded "
            f"| FormBody={encoded_form_body}"
        )
        _api_log(f"[外部API][debug] {skill_id} body={json.dumps(body, ensure_ascii=False)[:500]}", debug=True)
        result = call_service(url, "POST", body, timeout=service_timeout, as_form=True)
    business_error = _business_error(result.get("data")) if result.get("ok") else ""
    if business_error:
        result = {"ok": False, "data": [], "error": business_error}
    if result["ok"]:
        _api_log(f"[外部API] {skill_id} 返回成功 raw={len(result.get('data', []))}")
    else:
        _api_log(f"[外部API] {skill_id} 返回失败: {_clean_log_text(result.get('error', ''))}")

    if not result["ok"]:
        error = result.get("error", "外部接口调用失败")
        user_message = f"未取得{skill_label}接口数据，当前无法用该结构化数据源判断"
        return [{
            "file": f"{skill_label} (未取得数据)",
            "template": skill_label,
            "bindings": [
                {"html_field": "接口状态", "value": "未取得数据", "xml_path": "external/service_status"},
                {"html_field": "说明", "value": user_message, "xml_path": "external/service_message"},
            ],
            "visit_no": visit_no or "",
            "keep_fields": svc.get("keep_fields"),
            "rec_prefix": svc.get("rec_prefix", "记录"),
            "field_labels": svc.get("field_labels", {}),
            "merge": svc.get("merge", []),
            "temporal_semantics": svc.get("temporal_semantics", {}),
            "semantic": svc.get("semantic", {}),
            "service_error": True,
            "service_id": skill_id,
            "error": user_message,
            "debug_error": error,
        }]

    data = result["data"]
    _api_log(f"[外部API][debug] {skill_id} 原始返回类型: {type(data).__name__}, 长度: {len(data) if isinstance(data, list) else 'N/A'}", debug=True)
    if isinstance(data, list) and len(data) > 0:
        _api_log(f"[外部API][debug] {skill_id} 第一条keys: {list(data[0].keys()) if isinstance(data[0], dict) else type(data[0]).__name__}", debug=True)
    data = _normalize_response_records(data, skill_id)

    bindings_list = []
    for item in data:
        if not isinstance(item, dict):
            continue
        import re as _re3
        def _clean(val):
            s = str(val)
            # Remove control chars (0x00-0x1f, 0x7f-0x9f) except newline/tab
            s = _re3.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]', '', s)
            return s
        bindings = [
            {"html_field": str(k), "value": _clean(v), "xml_path": str(k)}
            for k, v in item.items()
        ]
        skill_id = svc.get("id", svc.get("name", "external"))
        skill_label = svc.get("label", svc.get("name", skill_id))
        bindings_list.append({
            "file": skill_label,
            "template": skill_label,
            "service_id": skill_id,
            "bindings": bindings,
            "visit_no": visit_no or "",
            "keep_fields": svc.get("keep_fields"),
            "rec_prefix": svc.get("rec_prefix", "记录"),
            "field_labels": svc.get("field_labels", {}),
            "merge": svc.get("merge", []),  # field merge rules from SKILL.md
            "temporal_semantics": svc.get("temporal_semantics", {}),
            "semantic": svc.get("semantic", {}),
        })

    # External API results: always merge multiple records into one before sending to LLM
    if bindings_list:
        merged = _merge_external_results(bindings_list)
        return [merged]
    return None


def _is_system_field(field: str) -> bool:
    """Pattern-based check: is this a technical/system field?"""
    if not field:
        return True
    # Known system fields (cross-API, never clinical)
    _known = {"businessFieldCode","businessFieldDesc","hdcPatientId","hdcEncId",
              "hosEncId","hosPatientId","agentContent","uri","success","dataCategory",
              "draw","pages","pageSize","recordsTotal","otherMsg","code","message",
              "seq","diagPrefixDesc","diagLevel",
              "hosPatRegNo","hdcPatRegNo","inpatientNo",
              "emrHosCode","emrHospitalCode","emrHospitalDesc",
              "patPayTypeDesc","patMediNo",
              "currBedNo","currRoomCode","currentRoom"}
    if field in _known:
        return True
    # Suffix patterns: IDs, codes, internal flags
    if field.endswith(("Id","Code")) and not field.endswith(("Name","Desc")):
        return True
    if field.endswith(("Flag","Seq","SeqNo","No","Grp","GrpFlag")):
        return True
    # Update/Stop metadata
    for kw in ("UpdateUser","UpdateDate","UpdateTime","StopDoc","StopDate","StopTime"):
        if field.startswith(("ord"+kw,"diag"+kw,"med"+kw,"insp"+kw)) or field.endswith(kw):
            return True
    # Other system patterns
    _sys_suffixes = ("DeptId","DeptCode","ExecDeptId","ExecDeptCode",
                     "ReqExecDeptCode","ReqExecDeptName","ReqExecDate","ReqExecTime",
                     "QuantityUint","PackQty","PackUnit","SkinTest","Urgent",
                     "Antibiotic","Regulatory","Servmaterial","BillCategoryCode",
                     "BillCategoryDesc","ResultStatCode","ResultStatDesc",
                     "StatusCode","StatusDesc","ParentId","GrpNo","StartDate","StartTime")
    if field.endswith(_sys_suffixes):
        return True
    return False


def _merge_external_results(bindings_list: list) -> dict:
    """Merge multiple API records into one binding — individual field lines like DB bindings."""
    # All per-service config comes from SKILL.md
    rec_prefix = bindings_list[0].get("rec_prefix", "记录")
    keep_fields = bindings_list[0].get("keep_fields", None)  # whitelist from SKILL.md
    field_labels = bindings_list[0].get("field_labels", {})   # eng→chn field name map

    multi = len(bindings_list) > 1  # whether to show record-number prefix

    merge_rules = bindings_list[0].get("merge", []) if bindings_list else []
    # Precompute set of field names that merge rules consume (to skip them from regular output)
    _merge_consume = set()
    for rule in merge_rules:
        if isinstance(rule, dict):
            for f in rule.get("fields", []):
                _merge_consume.add(f)

    lines = []
    kept_count = 0
    skipped_count = 0
    fallback_used = False
    all_api_fields = []
    for i, rec in enumerate(bindings_list):
        prefix = f"[{rec_prefix}{i+1}] " if multi else ""
        # ── Build per-record eng_field→value dict ──
        rec_fields: dict = {}
        for b in rec.get("bindings", []):
            field = b.get("html_field", "")
            val = str(b.get("value", ""))
            all_api_fields.append(field)
            if not val.strip():
                skipped_count += 1
                continue
            if keep_fields is not None:
                if field not in keep_fields and field not in _merge_consume:
                    skipped_count += 1
                    continue
            elif _is_system_field(field):
                skipped_count += 1
                continue
            if val.strip().startswith("[{") or val.strip().startswith("{"):
                skipped_count += 1
                continue
            rec_fields[field] = val

        if keep_fields is not None and not rec_fields:
            fallback_used = True
            rec_fields = {}
            for b in rec.get("bindings", []):
                field = b.get("html_field", "")
                val = str(b.get("value", ""))
                if not val.strip():
                    continue
                if _is_system_field(field):
                    continue
                if val.strip().startswith("[{") or val.strip().startswith("{"):
                    continue
                rec_fields[field] = val

        # ── Apply merge rules per record ──
        for rule in merge_rules:
            if not isinstance(rule, dict):
                continue
            fields = rule.get("fields", [])
            sep = rule.get("sep", " ")
            label = rule.get("name", sep.join(fields))
            vals = [rec_fields.get(f, "") for f in fields]
            if vals and any(v.strip() for v in vals):
                merged_val = sep.join(vals)
                # Remove individual source fields
                for f in fields:
                    rec_fields.pop(f, None)
                rec_fields[label] = merged_val

        # ── Emit lines for this record ──
        unmapped_index = 1
        for field, val in rec_fields.items():
            display_field = field_labels.get(field, field)
            if display_field == field and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', field):
                if fallback_used:
                    display_field = f"未映射字段{unmapped_index}"
                    unmapped_index += 1
                else:
                    skipped_count += 1
                    continue
            kept_count += 1
            lines.append({
                "html_field": prefix + display_field,
                "value": val,
                "xml_path": f"external/{field}",
                "eng_field": field,
            })

    skill_id = bindings_list[0].get("template", "external") if bindings_list else "external"
    if merge_rules:
        _api_log(f"[外部API][debug] {skill_id} merge: {len(bindings_list)}条记录, 规则数{len(merge_rules)}, 输出{len(lines)}字段", debug=True)

    fallback_note = "，字段映射降级" if fallback_used else ""
    _api_log(f"[外部API] {skill_id} 字段整理: 记录={len(bindings_list)} 输出字段={kept_count} 跳过={skipped_count}{fallback_note}")
    if keep_fields:
        _api_log(f"[外部API][debug] {skill_id} keep_fields白名单: {keep_fields}", debug=True)
        _api_log(f"[外部API][debug] {skill_id} API返回字段: {all_api_fields[:20]}", debug=True)
    return {
        "file": f"{bindings_list[0].get('file','')} ({len(bindings_list)}条)",
        "template": bindings_list[0].get("template", "external"),
        "service_id": bindings_list[0].get("service_id", ""),
        "bindings": lines,
        "visit_no": "",
        "keep_fields": keep_fields,
        "rec_prefix": rec_prefix,
        "field_labels": field_labels,
        "field_mapping_degraded": fallback_used,
        "temporal_semantics": bindings_list[0].get("temporal_semantics", {}),
        "semantic": bindings_list[0].get("semantic", {}),
    }
