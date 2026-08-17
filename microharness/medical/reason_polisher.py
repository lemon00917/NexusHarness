"""LLM-based user explanation layer for medical filter results.

The filter pipeline keeps deterministic judgement and structured evidence.
This module only rewrites already-produced evidence into user-facing Chinese,
and never changes matched/status/confidence fields.
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from decimal import Decimal, InvalidOperation
from typing import Any

from microharness.medical.display_text import naturalize_user_text, sanitize_user_text
from microharness.medical.evidence import build_overall_result
from microharness.medical.temporal_parser import operator_display, parse_numeric_comparison


def _clip(text: Any, limit: int = 500) -> str:
    if isinstance(text, dict):
        for key in ("证据解释", "用户解释", "解释", "reason", "理由", "text"):
            val = text.get(key)
            if isinstance(val, str) and val.strip():
                text = val
                break
        else:
            text = ""
    elif isinstance(text, list):
        text = "；".join(str(x) for x in text if x)
    text = naturalize_user_text(str(text or "")).strip()
    return text[:limit]


def _judgment_label(item: dict[str, Any]) -> str:
    label = str(item.get("判断状态") or "").strip()
    if label in {"符合", "不符合", "未提及", "无法判断"}:
        return label
    return {
        "MATCHED": "符合",
        "NOT_MATCHED": "不符合",
        "NOT_MENTIONED": "未提及",
        "UNKNOWN": "无法判断",
    }.get(str(item.get("status") or "").upper(), "符合" if item.get("matched") else "不符合")


def _condition_result(info: dict[str, Any]) -> dict[str, Any]:
    value = info.get("condition_result")
    return value if isinstance(value, dict) else {}


def _canonical_condition(info: dict[str, Any]) -> dict[str, Any]:
    return _condition_result(info) or info


def _public_fact_value(value: Any, *, depth: int = 0) -> Any:
    """Return a bounded JSON-safe fact value without knowing skill-specific fields."""
    if value in (None, "", [], {}):
        return None
    if isinstance(value, dict):
        if depth >= 3:
            return None
        return {
            str(key): normalized
            for key, item in list(value.items())[:40]
            if not str(key).startswith("_")
            and (normalized := _public_fact_value(item, depth=depth + 1)) is not None
        }
    if isinstance(value, (list, tuple, set)):
        if depth >= 3:
            return None
        return [
            normalized
            for item in list(value)[:30]
            if (normalized := _public_fact_value(item, depth=depth + 1)) is not None
        ]
    if isinstance(value, (str, int, float, bool)):
        return value[:600] if isinstance(value, str) else value
    return str(value)[:300]


def _public_fact_mapping(data: dict[str, Any]) -> dict[str, Any]:
    normalized = _public_fact_value(data)
    return normalized if isinstance(normalized, dict) else {}


def _evidence_fact(item: dict[str, Any]) -> dict[str, Any]:
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    fact = {
        "来源": item.get("source_name", ""),
        "来源角色": item.get("source_role", ""),
        "记录ID": item.get("record_id", ""),
        "文档": item.get("document", ""),
        "章节": item.get("section", ""),
        "实体": item.get("entity", ""),
        "事件时间": item.get("event_time"),
        "数值": item.get("value"),
        "单位": item.get("unit", ""),
        "异常标志": item.get("abnormal_flag", ""),
        "参考范围": item.get("reference_range", ""),
        "状态": item.get("status", ""),
        "原因码": item.get("reason_code", ""),
        "依据": _clip(item.get("reason"), 500),
    }
    fact["扩展事实"] = _public_fact_mapping(metadata)
    return {key: value for key, value in fact.items() if value not in (None, "", [], {})}


def _condition_fact_view(info: dict[str, Any]) -> dict[str, Any]:
    canonical = _condition_result(info)
    if not canonical:
        return {
            "状态": str(info.get("status") or ""),
            "判断状态": _judgment_label(info),
            "原因": _clip(info.get("reason"), 900),
            "时间范围": info.get("时间范围") or {},
        }
    decisions = []
    for item in canonical.get("source_decisions") or []:
        if not isinstance(item, dict):
            continue
        decisions.append(_public_fact_mapping(item))
    evidence = [
        _evidence_fact(item)
        for item in (canonical.get("evidence") or [])[:20]
        if isinstance(item, dict)
    ]
    return {
        "条件ID": canonical.get("condition_id", ""),
        "状态": canonical.get("status", ""),
        "判断状态": _judgment_label(canonical),
        "原因码": canonical.get("reason_code", ""),
        "原因": _clip(canonical.get("reason"), 900),
        "数据质量": canonical.get("data_quality", ""),
        "冲突级别": canonical.get("conflict_level", ""),
        "时间范围": info.get("时间范围") or {},
        "来源决策": decisions,
        "证据事实": evidence,
    }


def _source_identity(data: dict[str, Any]) -> str:
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    explicit = (
        data.get("logical_source_id")
        or data.get("evidence_source_id")
        or data.get("source_id")
        or metadata.get("logical_source_id")
        or metadata.get("evidence_source_id")
        or metadata.get("source_id")
    )
    if explicit:
        return str(explicit)
    service_id = data.get("service_id") or metadata.get("service_id")
    if service_id:
        return f"service:{service_id}"
    template = data.get("template") or metadata.get("template")
    if template:
        return f"document:{template}"
    return ""


def _condition_basis(info: dict[str, Any]) -> str:
    return json.dumps(_condition_fact_view(info), ensure_ascii=False, default=str)


def _query_connector(result: dict[str, Any], first: dict[str, Any], condition_count: int) -> str:
    query_ir = result.get("查询IR") if isinstance(result.get("查询IR"), dict) else {}
    route = result.get("route") if isinstance(result.get("route"), dict) else {}
    overall = first.get("overall_result") if isinstance(first.get("overall_result"), dict) else {}
    connector = (
        query_ir.get("连接关系")
        or route.get("connector")
        or overall.get("connector")
        or ("and" if condition_count > 1 else "single")
    )
    normalized = str(connector or "").strip().lower()
    if condition_count <= 1 or normalized == "single":
        return "SINGLE"
    return "OR" if normalized == "or" else "AND"


def _canonical_condition_results(first: dict[str, Any]) -> list[dict[str, Any]]:
    explicit = first.get("condition_results")
    if isinstance(explicit, list):
        conditions = [item for item in explicit if isinstance(item, dict)]
        if conditions:
            return conditions
    conditions = []
    for condition_text, info in (first.get("per_condition") or {}).items():
        if not isinstance(info, dict):
            continue
        canonical = dict(_canonical_condition(info))
        canonical.setdefault("condition", str(info.get("condition") or condition_text))
        if not canonical.get("status"):
            canonical["status"] = {
                "符合": "MATCHED",
                "不符合": "NOT_MATCHED",
                "未提及": "NOT_MENTIONED",
                "无法判断": "UNKNOWN",
            }[_judgment_label(info)]
        conditions.append(canonical)
    return conditions


def _overall_fact_view(result: dict[str, Any], first: dict[str, Any]) -> dict[str, Any]:
    conditions = _canonical_condition_results(first)
    connector = _query_connector(result, first, len(conditions))
    overall = build_overall_result(
        conditions,
        connector="or" if connector == "OR" else "and",
    )
    condition_facts = []
    for index, condition in enumerate(conditions, 1):
        facts = _condition_fact_view({"condition_result": condition})
        facts["条件"] = str(condition.get("condition") or f"条件{index}")
        condition_facts.append(facts)
    return {
        "连接关系": connector,
        "状态": overall["status"],
        "判断状态": overall["判断状态"],
        "原因码": overall["reason_code"],
        "组合依据": overall["reason"],
        "子条件": condition_facts,
    }


def _file_basis(file_info: dict[str, Any], condition_info: dict[str, Any]) -> str:
    source_name = str(file_info.get("source_name") or file_info.get("file") or "")
    canonical = _condition_result(condition_info)
    file_source_id = _source_identity(file_info)
    source_facts = [
        _evidence_fact(item)
        for item in canonical.get("evidence") or []
        if isinstance(item, dict)
        and (
            (file_source_id and _source_identity(item) == file_source_id)
            or (not file_source_id and str(item.get("source_name") or "") == source_name)
        )
    ]
    source_decisions = [
        item
        for item in canonical.get("source_decisions") or []
        if isinstance(item, dict)
        and (
            (file_source_id and str(item.get("source_id") or "") == file_source_id)
            or (not file_source_id and str(item.get("source_name") or "") == source_name)
        )
    ]
    return json.dumps({
        "来源": source_name,
        "状态": source_decisions[0].get("status", "") if source_decisions else file_info.get("status", ""),
        "原因码": source_decisions[0].get("reason_code", "") if source_decisions else file_info.get("reason_code", ""),
        "原因": _clip(source_decisions[0].get("reason"), 900) if source_decisions else _clip(file_info.get("reason"), 900),
        "来源决策": source_decisions[:1],
        "证据事实": source_facts[:12],
    }, ensure_ascii=False, default=str)


def _build_payload(result: dict[str, Any]) -> dict[str, Any]:
    first = (result.get("results") or [{}])[0] if isinstance(result.get("results"), list) else {}
    per_condition = first.get("per_condition") or {}
    conditions = []
    for idx, (cond_text, info) in enumerate(per_condition.items(), 1):
        canonical_info = _canonical_condition(info)
        files = []
        for fidx, file_info in enumerate(info.get("files") or [], 1):
            file_basis = json.loads(_file_basis(file_info, info))
            canonical_file_status = str(file_basis.get("状态") or "").upper()
            canonical_file = (
                {"status": canonical_file_status}
                if canonical_file_status
                else file_info
            )
            files.append({
                "id": f"C{idx}F{fidx}",
                "来源": file_info.get("file", ""),
                "证据角色": file_info.get("证据角色", ""),
                "用途": file_info.get("用途", ""),
                "判断状态": _judgment_label(canonical_file),
                "是否支持条件": (
                    canonical_file_status == "MATCHED"
                    if canonical_file_status
                    else bool(file_info.get("matched", False))
                ),
                "判定依据": _clip(file_basis.get("原因"), 900),
                "裁决事实": file_basis,
            })
        conditions.append({
            "id": f"C{idx}",
            "条件": cond_text,
            "判断状态": _judgment_label(canonical_info),
            "判定依据": _clip(canonical_info.get("reason"), 700),
            "时间范围": info.get("时间范围") or {},
            "证据源": files,
            "裁决事实": _condition_fact_view(info),
        })
    overall_facts = _overall_fact_view(result, first)
    return {
        "原始问题": result.get("condition", ""),
        "总体判断": overall_facts["判断状态"],
        "是否可判定": overall_facts["状态"] != "UNKNOWN",
        "置信度": result.get("置信度"),
        "总体判定依据": overall_facts["组合依据"],
        "总体裁决事实": overall_facts,
        "子条件": conditions,
    }


def _fallback_explanations(result: dict[str, Any]) -> None:
    first = (result.get("results") or [{}])[0] if isinstance(result.get("results"), list) else {}
    if isinstance(first, dict):
        for info in (first.get("per_condition") or {}).values():
            if isinstance(info, dict):
                info["用户解释"] = _condition_fallback_explanation(info)
                for file_info in info.get("files") or []:
                    if isinstance(file_info, dict):
                        file_info["用户解释"] = _file_fallback_explanation(file_info, info)
        first["用户解释"] = _overall_fallback_explanation(result, first)


def _overall_fallback_explanation(result: dict[str, Any], first: dict[str, Any]) -> str:
    overall_facts = _overall_fact_view(result, first)
    per_condition = first.get("per_condition") or {}
    if len(per_condition) <= 1:
        conditions = overall_facts.get("子条件") or []
        if conditions:
            reason = _clip(conditions[0].get("原因"), 800)
            prefix = f"总体判断：{overall_facts['判断状态']}。"
            return _clip(prefix + reason, 900)
        return _clip(overall_facts.get("组合依据"), 900)
    parts = []
    for idx, (cond_text, info) in enumerate(per_condition.items(), 1):
        if not isinstance(info, dict):
            continue
        canonical = _canonical_condition(info)
        status = _judgment_label(canonical)
        reason = _clip(info.get("用户解释") or canonical.get("reason"), 360)
        parts.append(f"条件{idx}「{cond_text}」：{status}。{reason}")
    if parts:
        relation = overall_facts["连接关系"]
        relation_text = "全部条件需同时满足" if relation == "AND" else "任一条件满足即可"
        prefix = f"总体判断：{overall_facts['判断状态']}。按{relation}关系（{relation_text}）组合，"
        return _clip(prefix + "；".join(parts), 1200)
    return _clip(overall_facts.get("组合依据"), 900)


def _condition_fallback_explanation(info: dict[str, Any]) -> str:
    files = [f for f in (info.get("files") or []) if isinstance(f, dict)]
    canonical = _canonical_condition(info)
    status = _judgment_label(canonical)
    if _condition_result(info):
        reason = _clip(canonical.get("reason"), 900)
        return _clip(f"该条件判定为{status}。{reason}" if reason else f"该条件判定为{status}。", 1000)
    if status == "符合":
        preferred_files = [f for f in files if f.get("matched") is True]
    elif status == "不符合":
        preferred_files = [f for f in files if f.get("matched") is False and not _source_unavailable_file(f)]
    elif status == "未提及":
        preferred_files = [
            f for f in files
            if str(f.get("status") or "").upper() == "NOT_MENTIONED"
            or any(token in str(f.get("reason") or "") for token in ("未找到", "未提及", "无匹配"))
        ]
        preferred_files = [f for f in preferred_files if not _source_unavailable_file(f)]
    else:
        preferred_files = [f for f in files if _source_unavailable_file(f)]
    preferred_files = preferred_files or files
    main_files = [f for f in preferred_files if f.get("证据角色") == "主证据"]
    source_files = main_files or preferred_files
    details = [_file_fallback_explanation(file_info, info) for file_info in source_files[:2]]
    details = [d for d in details if d]
    if details:
        return _clip(f"该条件判定为{status}。" + "；".join(details), 1000)
    return _clip(canonical.get("reason"), 900)


_SOURCE_UNAVAILABLE_TOKENS = ("未取得", "接口调用失败", "数据源调用失败", "数据源不可用", "服务不可用")


def _source_unavailable_file(file_info: dict[str, Any]) -> bool:
    reason = str(file_info.get("reason", "") or "")
    condition_result = file_info.get("condition_result") if isinstance(file_info.get("condition_result"), dict) else {}
    return (
        condition_result.get("reason_code") == "SOURCE_UNAVAILABLE"
        or any(token in reason for token in _SOURCE_UNAVAILABLE_TOKENS)
    )


def _condition_explanation_matches_status(text: str, info: dict[str, Any]) -> bool:
    """Reject explanations that replace a supported match with source-failure text."""
    status = _judgment_label(_canonical_condition(info))
    if status != "符合":
        return True
    has_support = any(
        isinstance(file_info, dict) and file_info.get("matched") is True
        for file_info in (info.get("files") or [])
    )
    if not has_support:
        return True
    explanation = _clip(text, 1000)
    return not any(token in explanation for token in _SOURCE_UNAVAILABLE_TOKENS + ("当前无法", "无法用该",))


def _overall_explanation_matches_status(
    text: str,
    first: dict[str, Any],
    result: dict[str, Any] | None = None,
) -> bool:
    overall_facts = _overall_fact_view(result or {}, first)
    status = overall_facts["判断状态"]
    explanation = _clip(text, 1200)
    overall_claims = re.findall(
        r"(?:总体判断|总体结论)\s*(?:为|是|：)\s*(符合|不符合|未提及|无法判断)",
        explanation,
    )
    if overall_claims and any(claim != status for claim in overall_claims):
        return False
    conditions = overall_facts.get("子条件") or []
    child_claims = {
        int(index): claim
        for index, claim in re.findall(
            r"条件\s*(\d+)(?:(?!条件\s*\d+)[^；。]){0,80}?(?:为|是|：)\s*"
            r"(符合|不符合|未提及|无法判断)",
            explanation,
        )
    }
    for index, condition in enumerate(conditions, 1):
        expected = str(condition.get("判断状态") or "")
        if child_claims.get(index) and child_claims[index] != expected:
            return False
    connector = overall_facts.get("连接关系")
    if connector == "AND" and any(token in explanation for token in (
        "任一条件满足即可", "任一条件符合即可", "任意一个条件满足即可", "只需一个条件满足",
    )):
        return False
    if connector == "OR" and any(token in explanation for token in (
        "全部条件需同时满足", "所有条件需同时满足", "必须同时满足全部条件", "均需满足",
    )):
        return False
    if isinstance(first.get("condition_results"), list) and len(conditions) > 1:
        for index, condition in enumerate(conditions, 1):
            terms = _condition_terms(f"{condition.get('条件', '')} {condition.get('原因', '')}")
            if f"条件{index}" not in explanation and (not terms or not any(term in explanation for term in terms)):
                return False
    if status != '符合':
        return True
    has_support = any(str(condition.get("状态") or "") == "MATCHED" for condition in conditions)
    if not has_support:
        return True
    return not any(token in explanation for token in _SOURCE_UNAVAILABLE_TOKENS + ('当前无法', '无法用该'))


def _file_fallback_explanation(file_info: dict[str, Any], condition_info: dict[str, Any] | None = None) -> str:
    source = str(file_info.get("file") or "证据源")
    reason = _clip(file_info.get("reason"), 900)
    records = file_info.get("候选记录") or []
    if records:
        return _candidate_record_explanation(source, file_info, condition_info or {})
    return reason


def _candidate_record_explanation(
    source: str,
    file_info: dict[str, Any],
    condition_info: dict[str, Any],
) -> str:
    records = [r for r in (file_info.get("候选记录") or []) if isinstance(r, dict)]
    if not records:
        return _clip(file_info.get("reason"), 900)

    tw = condition_info.get("时间范围") or file_info.get("时间范围") or {}
    window_text = ""
    if isinstance(tw, dict) and (tw.get("start") or tw.get("end")):
        window_text = f"目标时间范围为{tw.get('start') or '未知'}至{tw.get('end') or '未知'}。"
    elif "范围：" in str(file_info.get("reason", "")):
        match = re.search(r"范围：([^）：]+至[^）：]+)", str(file_info.get("reason", "")))
        if match:
            window_text = f"目标时间范围为{match.group(1)}。"

    outside = [r for r in records if r.get("是否在时间窗") is False]
    inside = [r for r in records if r.get("是否在时间窗") is True]
    missing_window = [r for r in records if r.get("是否在时间窗") not in (True, False)]

    examples = []
    for record in records[:3]:
        examples.append(_one_candidate_record_sentence(source, record, file_info))
    more = f"另有{len(records) - 3}条候选记录未展开。" if len(records) > 3 else ""

    if outside and not inside:
        conclusion = f"共找到{len(records)}条候选记录，但{len(outside)}条不在目标时间范围内。"
    elif inside:
        failed_numeric = [r for r in inside if r.get("数值是否满足") is False]
        passed_numeric = [r for r in inside if r.get("数值是否满足") is True]
        if passed_numeric:
            conclusion = f"共找到{len(records)}条候选记录，其中{len(passed_numeric)}条在时间范围内且满足条件。"
        elif failed_numeric:
            conclusion = f"共找到{len(records)}条候选记录，其中{len(failed_numeric)}条在时间范围内，但数值判断不满足条件。"
        else:
            conclusion = f"共找到{len(records)}条候选记录，其中{len(inside)}条在时间范围内。"
    elif missing_window:
        conclusion = f"共找到{len(records)}条候选记录，但缺少可比较的时间或时间范围，无法确认是否满足。"
    else:
        conclusion = f"共找到{len(records)}条候选记录。"

    return _clip(window_text + conclusion + "".join(examples) + more, 1200)


def _one_candidate_record_sentence(source: str, record: dict[str, Any], file_info: dict[str, Any] | None = None) -> str:
    prefix = str(record.get("记录") or "候选记录")
    enriched = {**_parse_record_fields(file_info or {}, prefix), **record}
    in_window = record.get("是否在时间窗")
    window_text = "在时间范围内" if in_window is True else "不在时间范围内" if in_window is False else "时间范围无法判断"
    pair_texts = _candidate_display_pairs(enriched)
    detail_text = "，".join(pair_texts[:8])
    detail_text = f"，{detail_text}" if detail_text else ""
    return f"{prefix}{detail_text}，{window_text}。"


_INTERNAL_RECORD_KEYS = {
    "记录",
    "记录序号",
    "记录ID",
    "记录标识名称",
    "记录标识字段",
    "是否在时间窗",
    "时间窗",
    "时间判断",
    "数值是否满足",
    "record_id",
    "record_ids",
    "record_id_label",
    "record_id_field",
    "record_id_fields",
}
_LOW_VALUE_LABEL_TOKENS = ("代码", "编号", "序号", "ID", "Id", "id", "科室", "医生", "医师", "人员")
_IDENTITY_LABEL_TOKENS = ("名称", "项目", "描述", "标题", "章节", "诊断", "药物", "医嘱")
_TIME_LABEL_TOKENS = ("时间", "日期")
_CLINICAL_DETAIL_TOKENS = ("类型", "类别", "途径", "方式", "剂型", "剂量", "单位", "频次", "结果", "标志", "范围", "判断")


def _candidate_display_pairs(data: dict[str, Any]) -> list[str]:
    result = str(data.get("结果") or "").strip()
    unit = str(data.get("单位") or "").strip()
    has_specific_time = any(
        key not in _INTERNAL_RECORD_KEYS
        and key != "记录时间"
        and any(token in str(key) for token in _TIME_LABEL_TOKENS)
        for key, value in data.items()
        if str(value or "").strip()
    )

    selected: list[tuple[int, str, str]] = []
    for idx, (key, raw_value) in enumerate(data.items()):
        key_text = str(key or "").strip()
        value = str(raw_value or "").strip()
        if not key_text or not value or key_text in _INTERNAL_RECORD_KEYS:
            continue
        if key_text == "记录时间" and has_specific_time:
            continue
        if key_text == "单位" and result:
            continue
        if any(token in key_text for token in _LOW_VALUE_LABEL_TOKENS):
            continue
        if key_text == "结果" and unit:
            value = f"{value}{unit}"

        rank = _candidate_label_rank(key_text)
        if rank is None:
            continue
        selected.append((rank, key_text, value))

    selected.sort(key=lambda item: item[0])
    pairs = []
    seen = set()
    for _, key, value in selected:
        marker = (key, value)
        if marker in seen:
            continue
        seen.add(marker)
        if key == "数值判断":
            pairs.append(value)
        else:
            pairs.append(f"{key}为{value}")
    return pairs


def _candidate_label_rank(label: str) -> int | None:
    if any(token in label for token in _IDENTITY_LABEL_TOKENS):
        return 10
    if any(token in label for token in _TIME_LABEL_TOKENS):
        return 20
    if any(token in label for token in ("途径", "方式", "类型", "类别", "结果", "判断")):
        return 30
    if any(token in label for token in _CLINICAL_DETAIL_TOKENS):
        return 40
    return None


def _parse_record_fields(file_info: dict[str, Any], record_prefix: str) -> dict[str, str]:
    fields = str(file_info.get("fields") or "")
    if not fields or not record_prefix:
        return {}
    line = next((line for line in fields.splitlines() if record_prefix in line), "")
    if not line:
        return {}
    parsed: dict[str, str] = {}
    for part in line.split("|"):
        match = re.match(r"^\s*(?:\[[^\]]+\]\s*)?([^:：]+)[:：]\s*(.*?)\s*$", part)
        if match:
            parsed[match.group(1).strip()] = match.group(2).strip()
    return parsed


def _first_present(data: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = data.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _useful_explanation(text: str, basis: str, condition: str = "") -> bool:
    text = _clip(text, 900)
    basis = _clip(basis, 900)
    condition = _clip(condition, 300)
    if len(text) < 18:
        return False
    if "：" in text and text.count("：") >= 3:
        return False
    if any(token in text for token in ("[检验", "[诊断", "[医嘱")):
        return False
    if condition and text.replace("的患者", "") == condition.replace("的患者", ""):
        return False
    required_groups = []
    if (
        "候选记录" in basis
        and "不在" in basis
        and any(token in basis for token in ("范围", "时间窗", "住院期间", "期间"))
    ):
        required_groups.append(("不在",))
        required_groups.append(("范围", "时间窗", "检测时间"))
    if any(token in basis for token in ("不在", "范围", "期间", "检测时间", "入院", "出院")):
        required_groups.append((
            "不在", "范围", "期间", "检测时间", "记录时间", "采集时间", "时间为",
            "检测到", "入院", "出院",
        ))
    if any(token in basis for token in ("结果", "异常标志", "高于", "低于", "参考范围")):
        required_groups.append(("结果", "异常", "高于", "低于", "参考范围"))
    if any(token in basis for token in ("无法判断", "失败", "缺少")):
        required_groups.append(("无法判断", "失败", "缺少"))
    if "共找到" in basis and "条" in basis:
        required_groups.append(("共找到", "条", "全部", "每条", "候选"))
    return all(any(token in text for token in group) for group in required_groups)


def _useful_overall_explanation(text: str) -> bool:
    explanation = _clip(text, 1200)
    if len(explanation) < 18:
        return False
    if explanation.count("：") >= 5:
        return False
    return not any(token in explanation for token in ("[检验", "[诊断", "[医嘱"))


_CONDITION_TERM_STOPWORDS = (
    "患者", "病人", "条件", "指标", "检验", "检查", "项目", "结果", "记录", "证据",
    "住院期间", "住院期内", "本次住院", "治疗期间", "就诊期间", "住院", "期间",
    "入院前", "入院后", "入院时", "出院前", "出院后", "出院时", "入院", "出院",
    "手术前", "手术后", "手术中", "术前", "术后", "术中", "手术",
    "当天", "当日", "小时内", "小时", "分钟", "天内", "日内", "天", "日", "周", "月", "个月",
    "使用过", "使用", "服用过", "服用", "注射过", "注射", "开了", "开具", "开过",
    "诊断为", "诊断", "做过", "进行过", "接受过",
    "大于等于", "小于等于", "大于", "小于", "超过", "高于", "低于", "以上", "以下",
    "偏高", "偏低", "升高", "降低", "异常", "不正常", "正常",
    "没有好转", "未好转", "好转", "缓解", "改善",
    "符合", "不符合", "未提及", "无法判断", "可判定", "不可判定",
)


def _condition_terms(text: Any) -> set[str]:
    """Extract discriminative condition terms for explanation alignment checks."""
    raw = _clip(text, 2000)
    if not raw:
        return set()
    normalized = re.sub(r"\[[^\]]+\]", " ", raw)
    normalized = re.sub(r"[0-9０-９一二两三四五六七八九十百千万亿〇零.]+", " ", normalized)
    chunks = re.findall(r"[\u4e00-\u9fffA-Za-z#%]+", normalized)
    terms: set[str] = set()
    for chunk in chunks:
        parts = [chunk]
        for stop in sorted(_CONDITION_TERM_STOPWORDS, key=len, reverse=True):
            next_parts: list[str] = []
            for part in parts:
                next_parts.extend(x for x in part.replace(stop, " ").split() if x)
            parts = next_parts
        for part in parts:
            part = part.strip()
            if len(part) >= 2 and part not in _CONDITION_TERM_STOPWORDS:
                terms.add(part)
    return terms


def _explanation_matches_condition(
    explanation: str,
    condition: str,
    basis: str,
    sibling_conditions: list[str],
) -> bool:
    """Reject LLM explanations that appear to belong to another subcondition."""
    text = _clip(explanation, 1000)
    if not text:
        return False
    current_terms = _condition_terms(f"{condition} {basis}")
    for sibling in sibling_conditions:
        for term in _condition_terms(sibling):
            if term not in current_terms and term in text:
                return False
    return True


def _combined_basis(first: dict[str, Any], result: dict[str, Any] | None = None) -> str:
    return json.dumps(
        _overall_fact_view(result or {}, first),
        ensure_ascii=False,
        default=str,
    )


def _opposite_comparison_tokens(operator: str) -> tuple[str, ...]:
    op = operator_display(operator)
    if op in {">", "≥"}:
        return ("低于", "小于", "少于", "不足", "不小于", "<", "≤")
    if op in {"<", "≤"}:
        return ("高于", "大于", "超过", "不大于", ">", "≥")
    return ()


def _contains_any_token(text: str, tokens: tuple[str, ...]) -> bool:
    return any(token and token in text for token in tokens)


_SUBTRACTION_EXPRESSION_RE = re.compile(
    r'([A-Za-z\u4e00-\u9fff][A-Za-z0-9_\u4e00-\u9fff]{1,19})'
    r'\s*[-\u2212\uff0d]\s*'
    r'([A-Za-z\u4e00-\u9fff][A-Za-z0-9_\u4e00-\u9fff]{1,19})'
)

_STATUS_CLAIM_RE = re.compile(
    r'(?:总体判断|总体结论|该条件|本条件|条件\d*)\s*(?:判定|判断|结论)?\s*(?:为|是|：)\s*'
    r'(符合|不符合|未提及|无法判断)'
)
_DATE_FACT_RE = re.compile(
    r'\d{4}(?:[-/.年]\d{1,2})(?:[-/.月]\d{1,2})日?(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?'
)
_NUMBER_FACT_RE = re.compile(r'(?<![A-Za-z])[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?')


def _explanation_status_claims_match(text: str, expected_status: str) -> bool:
    claims = _STATUS_CLAIM_RE.findall(_clip(text, 1200))
    return not claims or all(claim == expected_status for claim in claims)


def _normalized_date_facts(text: str) -> set[tuple[int, ...]]:
    facts: set[tuple[int, ...]] = set()
    for match in _DATE_FACT_RE.finditer(text or ""):
        parts = [int(value) for value in re.findall(r'\d+', match.group(0))]
        if len(parts) >= 3:
            facts.add(tuple(parts[:3]))
            if len(parts) >= 5:
                facts.add(tuple(parts[:5]))
            if len(parts) >= 6:
                facts.add(tuple(parts[:6]))
    return facts


def _normalized_number_facts(text: str) -> set[Decimal]:
    without_dates = _DATE_FACT_RE.sub(" ", text or "")
    facts: set[Decimal] = set()
    for token in _NUMBER_FACT_RE.findall(without_dates):
        try:
            facts.add(Decimal(token).normalize())
        except InvalidOperation:
            continue
    return facts


def _explanation_uses_only_known_numbers_and_dates(
    explanation: str,
    condition: str,
    basis: str,
) -> bool:
    text = _clip(explanation, 1200)
    known = f"{_clip(condition, 800)} {_clip(basis, 20000)}"
    if not _normalized_date_facts(text).issubset(_normalized_date_facts(known)):
        return False
    return _normalized_number_facts(text).issubset(_normalized_number_facts(known))


def _structured_basis(basis: str) -> Any:
    try:
        return json.loads(str(basis or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def _normalized_fact_token(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip().lower()
    text = text.replace("×", "x").replace("μ", "u").replace("µ", "u")
    return re.sub(r"[\s，,。；;：:\"'“”‘’「」『』()（）\[\]{}]", "", text)


def _fact_values_by_keys(data: Any, keys: set[str]) -> set[str]:
    values: set[str] = set()
    if isinstance(data, dict):
        for key, value in data.items():
            if str(key).lower() in keys:
                items = value if isinstance(value, (list, tuple, set)) else [value]
                values.update(
                    normalized
                    for item in items
                    if (normalized := _normalized_fact_token(item))
                )
            values.update(_fact_values_by_keys(value, keys))
    elif isinstance(data, (list, tuple, set)):
        for item in data:
            values.update(_fact_values_by_keys(item, keys))
    return values


_EXPLICIT_FACT_CLAIMS = {
    "SOURCE_NOT_IN_EVIDENCE": (
        re.compile(r"(?:数据源|证据源|来源)\s*(?:为|是|：|=)\s*[「『\"']?([^，。；\n」』\"']{1,80})"),
        {"来源", "source_name", "source_id"},
    ),
    "RECORD_ID_NOT_IN_EVIDENCE": (
        re.compile(r"(?:记录\s*ID|记录编号|record\s*id)\s*(?:为|是|：|=)\s*[「『\"']?([A-Za-z0-9_.:/-]{1,100})", re.I),
        {"记录id", "record_id", "record_ids"},
    ),
    "UNIT_NOT_IN_EVIDENCE": (
        re.compile(r"(?:单位)\s*(?:为|是|：|=)\s*[「『\"']?([^，。；\s\n」』\"']{1,40})"),
        {"单位", "unit", "dose_unit"},
    ),
    "ABNORMAL_FLAG_NOT_IN_EVIDENCE": (
        re.compile(r"(?:异常标志|异常标识)\s*(?:为|是|：|=)\s*[「『\"']?([^，。；\s\n」』\"']{1,40})"),
        {"异常标志", "abnormal_flag"},
    ),
    "REFERENCE_RANGE_NOT_IN_EVIDENCE": (
        re.compile(r"(?:参考范围|参考区间)\s*(?:为|是|：|=)\s*[「『\"']?([^，。；\n」』\"']{1,80})"),
        {"参考范围", "reference_range"},
    ),
}


def _structured_fact_violation_codes(explanation: str, condition: str, basis: str) -> list[str]:
    text = _clip(explanation, 1200)
    structured = _structured_basis(basis)
    codes: list[str] = []
    for code, (pattern, keys) in _EXPLICIT_FACT_CLAIMS.items():
        allowed = _fact_values_by_keys(structured, {key.lower() for key in keys})
        for claim in pattern.findall(text):
            normalized = _normalized_fact_token(claim)
            if normalized and normalized not in allowed:
                codes.append(code)
                break

    known_text = naturalize_user_text(f"{condition} {basis}")
    for quote in re.findall(
        r"(?:证据原文|记录原文|病历原文|原文)\s*(?:为|是|：|=)\s*[“\"「『]([^”\"」』]{2,300})[”\"」』]",
        text,
    ):
        if _normalized_fact_token(quote) not in _normalized_fact_token(known_text):
            codes.append("EVIDENCE_QUOTE_NOT_IN_FACTS")
            break
    return list(dict.fromkeys(codes))


def _subtraction_operand_pairs(text: str) -> set[tuple[str, str]]:
    normalized = re.sub(r'(?:约等于|等于)', '≈', text or '')
    return {
        (match.group(1), match.group(2))
        for match in _SUBTRACTION_EXPRESSION_RE.finditer(normalized)
        if match.group(1) != match.group(2)
    }


def _critical_fact_violation_codes(explanation: str, condition: str, basis: str) -> list[str]:
    """Reject display text that states facts not supported by evidence.

    This protects the deterministic result from LLM polishing drift. It is
    grammar/evidence based: no drug, diagnosis, or lab item names are encoded.
    """
    text = _clip(explanation, 1200)
    evidence = _clip(basis, 20000)
    condition_text = _clip(condition, 800)
    codes: list[str] = []

    known = f"{condition_text} {evidence}"
    if not _normalized_date_facts(text).issubset(_normalized_date_facts(known)):
        codes.append("DATE_NOT_IN_EVIDENCE")
    if not _normalized_number_facts(text).issubset(_normalized_number_facts(known)):
        codes.append("NUMBER_NOT_IN_EVIDENCE")

    cmp_info = parse_numeric_comparison(condition_text)
    if cmp_info:
        opposite_tokens = _opposite_comparison_tokens(cmp_info.operator)
        if _contains_any_token(text, opposite_tokens) and not _contains_any_token(evidence, opposite_tokens):
            codes.append("COMPARISON_DIRECTION_CHANGED")

    evidence_subtractions = _subtraction_operand_pairs(evidence)
    explanation_subtractions = _subtraction_operand_pairs(text)
    if any(
        (right_operand, left_operand) in explanation_subtractions
        for left_operand, right_operand in evidence_subtractions
    ):
        codes.append("SUBTRACTION_OPERANDS_REVERSED")

    absolute_absence = (
        "没有使用过", "未使用过", "没有用过", "未用过", "没有服用过", "未服用过",
        "没有开过", "未开过", "没有开具", "未开具", "没有使用", "未使用",
    )
    has_outside_window_candidate = (
        "找到" in evidence
        and ("候选记录" in evidence or "条" in evidence)
        and ("不在" in evidence or "时间窗" in evidence or "范围" in evidence)
    )
    if has_outside_window_candidate and _contains_any_token(text, absolute_absence):
        codes.append("TIME_WINDOW_CANDIDATE_CHANGED_TO_ABSENCE")

    codes.extend(_structured_fact_violation_codes(text, condition_text, evidence))
    return list(dict.fromkeys(codes))


def _explanation_preserves_critical_facts(explanation: str, condition: str, basis: str) -> bool:
    """Reject display text that states facts not supported by evidence.

    The checks consume the canonical evidence contract and arbitrary public
    metadata. They do not depend on any skill, medical entity, or source name.
    """
    return not _critical_fact_violation_codes(explanation, condition, basis)


def _unique_codes(codes: list[str]) -> list[str]:
    return list(dict.fromkeys(code for code in codes if code))


def _validate_overall_explanation(
    text: str,
    result: dict[str, Any],
    first: dict[str, Any],
) -> list[str]:
    explanation = _clip(text, 1200)
    if not explanation:
        return ["LLM_EXPLANATION_MISSING"]
    codes: list[str] = []
    if not _useful_overall_explanation(explanation):
        codes.append("FORMAT_OR_CONTENT_INSUFFICIENT")
    if not _overall_explanation_matches_status(explanation, first, result):
        codes.append("OVERALL_ADJUDICATION_MISMATCH")
    codes.extend(_critical_fact_violation_codes(
        explanation,
        str(result.get("condition") or ""),
        _combined_basis(first, result),
    ))
    return _unique_codes(codes)


def _validate_condition_explanation(
    text: str,
    condition: str,
    info: dict[str, Any],
    siblings: list[str],
) -> list[str]:
    explanation = _clip(text, 1200)
    if not explanation:
        return ["LLM_EXPLANATION_MISSING"]
    basis = _condition_basis(info)
    codes: list[str] = []
    if not _useful_explanation(explanation, basis, condition):
        codes.append("FORMAT_OR_CONTENT_INSUFFICIENT")
    if not _explanation_matches_condition(explanation, condition, basis, siblings):
        codes.append("CONDITION_SCOPE_MISMATCH")
    if not _explanation_status_claims_match(
        explanation,
        _judgment_label(_canonical_condition(info)),
    ):
        codes.append("STATUS_MISMATCH")
    if not _condition_explanation_matches_status(explanation, info):
        codes.append("SUPPORTED_MATCH_REPLACED_BY_SOURCE_FAILURE")
    codes.extend(_critical_fact_violation_codes(explanation, condition, basis))
    return _unique_codes(codes)


def _validate_source_explanation(
    text: str,
    condition: str,
    info: dict[str, Any],
    file_info: dict[str, Any],
    siblings: list[str],
) -> list[str]:
    explanation = _clip(text, 1200)
    if not explanation:
        return ["LLM_EXPLANATION_MISSING"]
    basis = _file_basis(file_info, info)
    fact_view = _structured_basis(basis)
    canonical_status = str(fact_view.get("状态") or "").upper() if isinstance(fact_view, dict) else ""
    expected_status = _judgment_label(
        {"status": canonical_status} if canonical_status else file_info
    )
    codes: list[str] = []
    if not _useful_explanation(explanation, basis, str(file_info.get("file") or "")):
        codes.append("FORMAT_OR_CONTENT_INSUFFICIENT")
    if not _explanation_matches_condition(explanation, condition, basis, siblings):
        codes.append("CONDITION_SCOPE_MISMATCH")
    if not _explanation_status_claims_match(explanation, expected_status):
        codes.append("STATUS_MISMATCH")
    codes.extend(_critical_fact_violation_codes(explanation, condition, basis))
    return _unique_codes(codes)


def _explanation_audit(scope: str, reason_codes: list[str]) -> dict[str, Any]:
    codes = _unique_codes(reason_codes)
    return {
        "scope": scope,
        "accepted": not codes,
        "used_fallback": bool(codes),
        "reason_codes": codes,
    }


def _set_fallback_audits(result: dict[str, Any], reason_code: str) -> None:
    first = (result.get("results") or [{}])[0] if isinstance(result.get("results"), list) else {}
    if not isinstance(first, dict):
        return
    first["解释校验"] = _explanation_audit("overall", [reason_code])
    for info in (first.get("per_condition") or {}).values():
        if not isinstance(info, dict):
            continue
        info["解释校验"] = _explanation_audit("condition", [reason_code])
        for file_info in info.get("files") or []:
            if isinstance(file_info, dict):
                file_info["解释校验"] = _explanation_audit("source", [reason_code])


def polish_response_explanations(
    result: dict[str, Any],
    model: str = "qwen2.5:3b",
    timeout: int = 30,
) -> dict[str, Any]:
    """Attach LLM-written `用户解释` fields without changing judgement fields."""
    if not isinstance(result, dict):
        return result
    if str(os.environ.get("MEDICAL_QUERY_POLISH", "1")).lower() in {"0", "false", "no", "off"}:
        _fallback_explanations(result)
        _set_fallback_audits(result, "LLM_EXPLANATION_DISABLED")
        return result

    _fallback_explanations(result)
    payload = _build_payload(result)
    if not payload.get("子条件"):
        return result

    prompt = f"""你是病历筛选系统的展示解释器。
任务：把机器判定依据改写成给医生/质控人员看的中文说明。

严格要求：
1. 只能基于输入中的“裁决事实”改写，不能新增事实、诊断、建议或推测。
2. 不改变符合/不符合/未提及/无法判断的结论。“未提及”表示数据源查询成功但没有目标实体或候选记录，不能改写成“不符合”或“无法判断”。
3. 总体解释必须遵守“总体裁决事实”的连接关系，并逐一覆盖每个子条件；AND表示全部条件同时满足，OR表示任一条件满足。
4. 不输出英文内部枚举、代码字段名、JSON字段名。
5. 语言要短、清楚、可复核；日期、数值、单位、异常标志、来源和记录只能使用“裁决事实”中已有内容。
6. 不要照抄机器原文，不要保留“[检验26]”这类内部编号，不要写成“字段：值，字段：值”的流水账。
7. 写成自然中文句子。例如：
   - “检验接口找到血红蛋白记录，但检测时间为2026-03-13 13:15:33，晚于本次住院结束时间2026-03-05 12:00:00，因此不能证明住院期间血红蛋白偏高；该结果本身还低于参考范围。”
8. 输出严格JSON：
{{
  "总体解释": "...",
  "子条件解释": {{"C1": "..."}},
  "证据解释": {{"C1F1": "..."}}
}}

输入证据：
{json.dumps(payload, ensure_ascii=False)}
"""
    try:
        from microharness.ollama import OllamaClient
        from microharness.medical.query_router import parse_llm_json

        client = OllamaClient(
            model=model or "qwen2.5:3b",
            timeout=timeout,
            format_json=True,
            num_predict=512,
        )
        raw = client.chat([{"role": "user", "content": prompt}], temperature=0.0)
        data = parse_llm_json(raw, context="用户解释润色")
        overall = _clip(data.get("总体解释"), 900)
        cond_map = data.get("子条件解释") if isinstance(data.get("子条件解释"), dict) else {}
        file_map = data.get("证据解释") if isinstance(data.get("证据解释"), dict) else {}
        first = (result.get("results") or [{}])[0]
        overall_codes = _validate_overall_explanation(overall, result, first)
        first["解释校验"] = _explanation_audit("overall", overall_codes)
        if not overall_codes:
            first["用户解释"] = overall
        per_items = list((first.get("per_condition") or {}).items())
        for cidx, (condition_key, info) in enumerate(per_items, 1):
            if not isinstance(info, dict):
                continue
            current_condition = str(info.get("condition") or condition_key or "")
            siblings = [str(key) for idx, (key, _) in enumerate(per_items, 1) if idx != cidx]
            cond_text = _clip(cond_map.get(f"C{cidx}"), 700)
            condition_codes = _validate_condition_explanation(
                cond_text,
                current_condition,
                info,
                siblings,
            )
            info["解释校验"] = _explanation_audit("condition", condition_codes)
            if not condition_codes:
                info["用户解释"] = cond_text
            best_file_text = ""
            for fidx, file_info in enumerate(info.get("files") or [], 1):
                if not isinstance(file_info, dict):
                    continue
                file_text = _clip(file_map.get(f"C{cidx}F{fidx}"), 600)
                source_codes = _validate_source_explanation(
                    file_text,
                    current_condition,
                    info,
                    file_info,
                    siblings,
                )
                file_info["解释校验"] = _explanation_audit("source", source_codes)
                if not source_codes:
                    file_info["用户解释"] = file_text
                role = str(file_info.get("证据角色", ""))
                if role == "主证据" and not best_file_text and _useful_explanation(
                    str(file_info.get("用户解释", "")),
                    str(file_info.get("reason", "")),
                    str(file_info.get("file", "")),
                ):
                    best_file_text = str(file_info.get("用户解释", ""))
            if best_file_text and not _useful_explanation(
                str(info.get("用户解释", "")),
                str(info.get("reason", "")),
                str(info.get("condition", "")),
            ):
                info["用户解释"] = best_file_text
    except Exception as exc:
        first = (result.get("results") or [{}])[0] if isinstance(result.get("results"), list) else {}
        if isinstance(first, dict):
            first["解释生成"] = f"LLM解释生成失败，已使用规则说明：{str(exc)[:80]}"
        _set_fallback_audits(result, "LLM_EXPLANATION_ERROR")
    return result
