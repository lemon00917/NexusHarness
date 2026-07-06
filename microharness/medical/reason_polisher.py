"""LLM-based user explanation layer for medical filter results.

The filter pipeline keeps deterministic judgement and structured evidence.
This module only rewrites already-produced evidence into user-facing Chinese,
and never changes matched/status/confidence fields.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from microharness.medical.display_text import naturalize_user_text, sanitize_user_text
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


def _build_payload(result: dict[str, Any]) -> dict[str, Any]:
    first = (result.get("results") or [{}])[0] if isinstance(result.get("results"), list) else {}
    per_condition = first.get("per_condition") or {}
    conditions = []
    for idx, (cond_text, info) in enumerate(per_condition.items(), 1):
        files = []
        for fidx, file_info in enumerate(info.get("files") or [], 1):
            files.append({
                "id": f"C{idx}F{fidx}",
                "来源": file_info.get("file", ""),
                "证据角色": file_info.get("证据角色", ""),
                "用途": file_info.get("用途", ""),
                "是否支持条件": bool(file_info.get("matched", False)),
                "判定依据": _clip(file_info.get("reason"), 600),
            })
        conditions.append({
            "id": f"C{idx}",
            "条件": cond_text,
            "判断状态": info.get("判断状态") or ("符合" if info.get("matched") else "不符合"),
            "判定依据": _clip(info.get("reason"), 700),
            "时间范围": info.get("时间范围") or {},
            "证据源": files,
        })
    return {
        "原始问题": result.get("condition", ""),
        "总体判断": result.get("判断状态") or first.get("判断状态", ""),
        "是否可判定": result.get("可判定"),
        "置信度": result.get("置信度"),
        "总体判定依据": _clip(first.get("reason"), 900),
        "子条件": conditions,
    }


def _fallback_explanations(result: dict[str, Any]) -> None:
    first = (result.get("results") or [{}])[0] if isinstance(result.get("results"), list) else {}
    if isinstance(first, dict):
        first["用户解释"] = _overall_fallback_explanation(result, first)
        for info in (first.get("per_condition") or {}).values():
            if isinstance(info, dict):
                info["用户解释"] = _condition_fallback_explanation(info)
                for file_info in info.get("files") or []:
                    if isinstance(file_info, dict):
                        file_info["用户解释"] = _file_fallback_explanation(file_info, info)


def _overall_fallback_explanation(result: dict[str, Any], first: dict[str, Any]) -> str:
    per_condition = first.get("per_condition") or {}
    if len(per_condition) <= 1:
        return _clip(first.get("reason"), 900)
    parts = []
    for idx, (cond_text, info) in enumerate(per_condition.items(), 1):
        if not isinstance(info, dict):
            continue
        status = info.get("判断状态") or ("符合" if info.get("matched") else "不符合")
        reason = _clip(info.get("reason"), 260)
        parts.append(f"条件{idx}「{cond_text}」：{status}。{reason}")
    if parts:
        overall = first.get("判断状态") or result.get("判断状态") or ""
        prefix = f"总体判断：{overall}。" if overall else ""
        return _clip(prefix + "；".join(parts), 1200)
    return _clip(first.get("reason"), 900)


def _condition_fallback_explanation(info: dict[str, Any]) -> str:
    files = info.get("files") or []
    main_files = [f for f in files if isinstance(f, dict) and f.get("证据角色") == "主证据"]
    source_files = main_files or [f for f in files if isinstance(f, dict)]
    details = [_file_fallback_explanation(file_info, info) for file_info in source_files[:2]]
    details = [d for d in details if d]
    status = info.get("判断状态") or ("符合" if info.get("matched") else "不符合")
    if details:
        return _clip(f"该条件判定为{status}。" + "；".join(details), 1000)
    return _clip(info.get("reason"), 900)


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

    if "用药" in source:
        name = _first_present(enriched, ("药物名称", "医嘱名称", "项目")) or "候选用药"
        time = _first_present(enriched, ("开立日期时间", "记录时间", "医嘱时间", "执行时间")) or "缺少开立时间"
        route = _first_present(enriched, ("用药途径", "给药途径"))
        route_text = f"，用药途径为{route}" if route else ""
        return f"{prefix}为{name}，开立时间{time}{route_text}，{window_text}。"

    if "诊断" in source:
        name = _first_present(enriched, ("诊断名称", "项目")) or "候选诊断"
        diag_type = _first_present(enriched, ("诊断类型", "诊断类别"))
        time = _first_present(enriched, ("诊断时间", "诊断日期", "记录时间")) or "缺少诊断时间"
        type_text = f"，诊断类型为{diag_type}" if diag_type else ""
        return f"{prefix}为{name}{type_text}，诊断时间{time}，{window_text}。"

    if "检验" in source:
        item = _first_present(enriched, ("项目", "化验项目描述")) or "候选检验项目"
        time = _first_present(enriched, ("检测时间", "记录时间")) or "缺少检测时间"
        result = _first_present(enriched, ("结果",))
        unit = _first_present(enriched, ("单位",))
        numeric = _first_present(enriched, ("数值判断",))
        result_text = f"，结果{result}{unit or ''}" if result else ""
        numeric_text = f"，{numeric}" if numeric else ""
        return f"{prefix}为{item}，检测时间{time}{result_text}，{window_text}{numeric_text}。"

    time = _first_present(enriched, ("记录时间", "检测时间", "开立日期时间", "诊断时间"))
    time_text = f"，记录时间{time}" if time else ""
    return f"{prefix}{time_text}，{window_text}。"


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
    if any(token in basis for token in ("不在", "范围", "期间", "检测时间", "入院", "出院")):
        required_groups.append(("不在", "范围", "期间", "检测时间", "入院", "出院"))
    if any(token in basis for token in ("结果", "异常标志", "高于", "低于", "参考范围")):
        required_groups.append(("结果", "异常", "高于", "低于", "参考范围"))
    if any(token in basis for token in ("无法判断", "失败", "缺少")):
        required_groups.append(("无法判断", "失败", "缺少"))
    if "共找到" in basis and "条" in basis:
        required_groups.append(("共找到", "条", "全部", "每条", "候选"))
    return all(any(token in text for token in group) for group in required_groups)


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
    "符合", "不符合", "无法判断", "可判定", "不可判定",
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


def _combined_basis(first: dict[str, Any]) -> str:
    parts = [str(first.get("reason", "") or "")]
    for info in (first.get("per_condition") or {}).values():
        if not isinstance(info, dict):
            continue
        parts.append(str(info.get("reason", "") or ""))
        for file_info in info.get("files") or []:
            if isinstance(file_info, dict):
                parts.append(str(file_info.get("reason", "") or ""))
    return "；".join(part for part in parts if part)


def _opposite_comparison_tokens(operator: str) -> tuple[str, ...]:
    op = operator_display(operator)
    if op in {">", "≥"}:
        return ("低于", "小于", "少于", "不足", "不小于", "<", "≤")
    if op in {"<", "≤"}:
        return ("高于", "大于", "超过", "不大于", ">", "≥")
    return ()


def _contains_any_token(text: str, tokens: tuple[str, ...]) -> bool:
    return any(token and token in text for token in tokens)


def _explanation_preserves_critical_facts(explanation: str, condition: str, basis: str) -> bool:
    """Reject display text that states facts not supported by evidence.

    This protects the deterministic result from LLM polishing drift. It is
    grammar/evidence based: no drug, diagnosis, or lab item names are encoded.
    """
    text = _clip(explanation, 1200)
    evidence = _clip(basis, 4000)
    condition_text = _clip(condition, 800)

    cmp_info = parse_numeric_comparison(condition_text)
    if cmp_info:
        opposite_tokens = _opposite_comparison_tokens(cmp_info.operator)
        if _contains_any_token(text, opposite_tokens) and not _contains_any_token(evidence, opposite_tokens):
            return False

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
        return False

    return True


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
        return result

    _fallback_explanations(result)
    payload = _build_payload(result)
    if not payload.get("子条件"):
        return result

    prompt = f"""你是病历筛选系统的展示解释器。
任务：把机器判定依据改写成给医生/质控人员看的中文说明。

严格要求：
1. 只能基于输入证据改写，不能新增事实、诊断、建议或推测。
2. 不改变符合/不符合/无法判断的结论。
3. 不输出英文内部枚举、代码字段名、JSON字段名。
4. 语言要短、清楚、可复核；必须说明导致结论的关键证据，例如时间范围、检测时间、结果值、异常标志、数据源。
5. 不要照抄机器原文，不要保留“[检验26]”这类内部编号，不要写成“字段：值，字段：值”的流水账。
6. 写成自然中文句子。例如：
   - “检验接口找到血红蛋白记录，但检测时间为2026-03-13 13:15:33，晚于本次住院结束时间2026-03-05 12:00:00，因此不能证明住院期间血红蛋白偏高；该结果本身还低于参考范围。”
7. 输出严格JSON：
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
        overall_basis = _combined_basis(first)
        if (
            overall
            and _useful_explanation(overall, overall_basis, result.get("condition", ""))
            and _explanation_preserves_critical_facts(overall, result.get("condition", ""), overall_basis)
        ):
            first["用户解释"] = overall
        per_items = list((first.get("per_condition") or {}).items())
        for cidx, (condition_key, info) in enumerate(per_items, 1):
            if not isinstance(info, dict):
                continue
            current_condition = str(info.get("condition") or condition_key or "")
            siblings = [str(key) for idx, (key, _) in enumerate(per_items, 1) if idx != cidx]
            cond_text = _clip(cond_map.get(f"C{cidx}"), 700)
            if (
                cond_text
                and _useful_explanation(cond_text, info.get("reason", ""), current_condition)
                and _explanation_matches_condition(cond_text, current_condition, info.get("reason", ""), siblings)
                and _explanation_preserves_critical_facts(cond_text, current_condition, info.get("reason", ""))
            ):
                info["用户解释"] = cond_text
            best_file_text = ""
            for fidx, file_info in enumerate(info.get("files") or [], 1):
                if not isinstance(file_info, dict):
                    continue
                file_text = _clip(file_map.get(f"C{cidx}F{fidx}"), 600)
                if (
                    file_text
                    and _useful_explanation(file_text, file_info.get("reason", ""), str(file_info.get("file", "")))
                    and _explanation_matches_condition(file_text, current_condition, file_info.get("reason", ""), siblings)
                    and _explanation_preserves_critical_facts(file_text, current_condition, file_info.get("reason", ""))
                ):
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
        if not _useful_explanation(
            str(first.get("用户解释", "")),
            _combined_basis(first),
            str(result.get("condition", "")),
        ) or not _explanation_preserves_critical_facts(
            str(first.get("用户解释", "")),
            str(result.get("condition", "")),
            _combined_basis(first),
        ):
            first["用户解释"] = _overall_fallback_explanation(result, first)
    except Exception as exc:
        first = (result.get("results") or [{}])[0] if isinstance(result.get("results"), list) else {}
        if isinstance(first, dict):
            first["解释生成"] = f"LLM解释生成失败，已使用规则说明：{str(exc)[:80]}"
    return result
