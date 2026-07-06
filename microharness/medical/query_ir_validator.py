"""Deterministic validation and repair for medical query analysis.

LLMs provide a useful first-pass structure, but they may drop temporal context
or rewrite numeric/unit expressions. This module keeps those repairs in one
place so execution code does not accumulate query-specific patches.
"""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Callable, Optional

from microharness.medical.semantic_rules import split_compound_clauses
from microharness.medical.temporal_parser import parse_numeric_comparison


_VALUE_PREDICATE_RE = re.compile(
    r"(>|<|>=|<=|=|≥|≤|＞|＜|大于|小于|高于|低于|超过|不少于|不低于|不超过|至多|至少|等于|偏高|偏低|异常|正常)"
)


def has_explicit_value_predicate(condition: str) -> bool:
    """True when a number is part of a value judgement, not just a time phrase."""
    return bool(_VALUE_PREDICATE_RE.search(condition or ""))


def is_executable_numeric_condition(condition: str) -> bool:
    """Whether the condition should be executed as a numeric comparison."""
    return bool(parse_numeric_comparison(condition) and has_explicit_value_predicate(condition))


def _constraint_issues(source: str, candidate: str) -> list[dict]:
    """Return generic semantic constraints that candidate dropped or changed."""
    issues: list[dict] = []
    source = source or ""
    candidate = candidate or ""

    src_cmp = parse_numeric_comparison(source)
    cand_cmp = parse_numeric_comparison(candidate)
    if src_cmp and not cand_cmp:
        issues.append({"code": "numeric_comparison_missing", "message": "原句包含数值比较，IR子条件未保留"})
    elif src_cmp and cand_cmp:
        if src_cmp.operator != cand_cmp.operator:
            issues.append({"code": "numeric_operator_changed", "message": "数值比较方向被改写"})
        tolerance = max(1e-9, abs(float(src_cmp.threshold)) * 1e-9)
        if abs(float(src_cmp.threshold) - float(cand_cmp.threshold)) > tolerance:
            issues.append({"code": "numeric_threshold_changed", "message": "数值比较阈值被改写"})

    if _has_time_expression(source) and not _has_time_expression(candidate):
        issues.append({"code": "temporal_expression_missing", "message": "原句包含时间表达，IR子条件未保留"})

    src_units = _unit_tokens(source)
    cand_units = _unit_tokens(candidate)
    if src_units and not src_units.issubset(cand_units):
        issues.append({"code": "unit_missing", "message": "原句包含单位，IR子条件未完整保留"})

    src_neg = _negation_tokens(source)
    cand_neg = _negation_tokens(candidate)
    if src_neg and not src_neg.issubset(cand_neg):
        issues.append({"code": "negation_missing", "message": "原句包含否定语义，IR子条件未完整保留"})

    return issues


def _has_time_expression(text: str) -> bool:
    if not text:
        return False
    try:
        from microharness.medical.time_window import requires_period_window

        if requires_period_window(text):
            return True
    except Exception:
        pass
    num = r"(?:\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百千万亿半]+)"
    unit = r"(?:分钟|小时|天|日|周|月|个月|年)"
    return bool(
        re.search(r"(住院期间|住院期内|本次住院|入院前|入院后|出院前|出院后|术前|术后|手术前|手术后|术中|手术中)", text)
        or re.search(rf"(?:前|后)?\s*{num}\s*{unit}\s*(?:内|前|后|以上|以下)?", text)
    )


def _unit_tokens(text: str) -> set[str]:
    """Extract generic unit fragments without knowing concrete lab names."""
    if not text:
        return set()
    units = set()
    for match in re.finditer(
        r"(?:x|\*|×)?10(?:\^?[+-]?\d+|[⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻]+)?/[A-Za-z一-龥%]+",
        text,
        re.I,
    ):
        units.add(match.group(0).replace("×", "x").replace("*", "x").lower())
    for match in re.finditer(r"\b(?:mg|g|mmol|umol|μmol|mol|ml|l|iu|u)/(?:dl|l|ml)\b", text, re.I):
        units.add(match.group(0).lower())
    for match in re.finditer(r"(?<=\d)\s*(?:%|岁|天|日|小时|分钟|月|年|次|个)", text):
        units.add(match.group(0).strip())
    return units


def _negation_tokens(text: str) -> set[str]:
    if not text:
        return set()
    cleaned = re.sub(r"不(?:低于|少于|超过|高于|大于|小于)", "", text)
    cleaned = re.sub(r"不正常", "", cleaned)
    return set(re.findall(r"否认|未见|没有|无|未|不", cleaned))


def enforce_compound_structure(
    analysis: dict,
    original_condition: str,
    fallback_keyword_fn: Optional[Callable[[str], str]] = None,
) -> dict:
    """Keep explicit AND/OR clause count aligned with the user sentence."""
    repaired = deepcopy(analysis or {})
    parts, connector = split_compound_clauses(original_condition)
    if len(parts) <= 1:
        return repaired

    conditions = list(repaired.get("conditions", []) or [])
    mismatch = len(conditions) != len(parts)
    connector_changed = connector and repaired.get("connector") not in (None, "", connector)
    if not mismatch and not connector_changed:
        if connector and not repaired.get("connector"):
            repaired["connector"] = connector
            repaired["type"] = "compound"
            _append_source(repaired, "connector_preserve")
        return repaired

    rebuilt = []
    for part in parts:
        rebuilt.append(
            {
                "text": part,
                "keyword": fallback_keyword_fn(part) if fallback_keyword_fn else part,
                "modifiers": [],
                "is_numeric": is_executable_numeric_condition(part),
                "target_docs": [],
                "target_sections": [],
                "target_skills": [],
            }
        )
    repaired["conditions"] = rebuilt
    repaired["connector"] = connector
    repaired["type"] = "compound"
    _append_source(repaired, "compound_structure_preserve")
    issues = repaired.setdefault("ir_validation", {}).setdefault("issues", [])
    issues.append(
        {
            "code": "compound_structure_repaired",
            "message": f"原句显式拆分为{len(parts)}个子条件，已按原句重建IR子条件",
        }
    )
    return repaired


def preserve_literal_clause_texts(
    analysis: dict,
    original_condition: str,
    fallback_keyword_fn: Optional[Callable[[str], str]] = None,
) -> dict:
    """Keep user-written numbers/units in split clauses.

    If an LLM correctly splits a compound query but rewrites scientific notation
    or units, downstream deterministic parsing must see the normalized user text,
    not the paraphrase.
    """
    repaired = deepcopy(analysis or {})
    parts, connector = split_compound_clauses(original_condition)
    conditions = list(repaired.get("conditions", []) or [])
    if len(parts) < 2 or len(parts) != len(conditions):
        return repaired

    changed = False
    for cond, literal in zip(conditions, parts):
        old_text = str(cond.get("text", ""))
        if old_text == literal:
            continue
        cond["text"] = literal
        if not cond.get("keyword") or cond.get("keyword") == old_text:
            cond["keyword"] = fallback_keyword_fn(literal) if fallback_keyword_fn else literal
        cond["is_numeric"] = is_executable_numeric_condition(literal)
        changed = True

    if changed:
        repaired["conditions"] = conditions
        repaired["connector"] = repaired.get("connector") or connector
        repaired["type"] = "compound"
        _append_source(repaired, "literal_preserve")
    return repaired


def preserve_single_temporal_condition(
    analysis: dict,
    original_condition: str,
    fallback_keyword_fn: Optional[Callable[[str], str]] = None,
) -> dict:
    """Prevent target-only analyzer output from dropping temporal context.

    Example: "术后24小时内开了维生素" may be analyzed as "开了维生素".
    The executable condition must keep the original temporal phrase so the
    time-window resolver can bind the event anchor.
    """
    repaired = deepcopy(analysis or {})
    try:
        from microharness.medical.time_window import requires_period_window
    except Exception:
        return repaired

    conditions = list(repaired.get("conditions", []) or [])
    if len(conditions) != 1:
        return repaired

    original_needs_time = requires_period_window(original_condition)
    current_text = str(conditions[0].get("text", "") or "")
    current_needs_time = requires_period_window(current_text)
    if not original_needs_time or current_needs_time:
        return repaired

    conditions[0]["text"] = original_condition
    conditions[0]["keyword"] = fallback_keyword_fn(original_condition) if fallback_keyword_fn else original_condition
    conditions[0]["is_numeric"] = is_executable_numeric_condition(original_condition)
    repaired["conditions"] = conditions
    issues = repaired.setdefault("ir_validation", {}).setdefault("issues", [])
    issues.append(
        {
            "condition_index": 0,
            "code": "temporal_expression_missing",
            "message": "原句包含时间表达，已按原句回填IR子条件",
        }
    )
    _append_source(repaired, "temporal_context_preserve")
    return repaired


def validate_clause_constraints(
    analysis: dict,
    original_condition: str,
    fallback_keyword_fn: Optional[Callable[[str], str]] = None,
) -> dict:
    """Validate that each executable clause preserves critical source grammar."""
    repaired = deepcopy(analysis or {})
    conditions = list(repaired.get("conditions", []) or [])
    if not conditions:
        return repaired

    parts, _ = split_compound_clauses(original_condition)
    source_clauses = parts if len(parts) == len(conditions) else [original_condition] if len(conditions) == 1 else []
    if not source_clauses:
        return repaired

    issues = repaired.setdefault("ir_validation", {}).setdefault("issues", [])
    changed = False
    for index, (source, cond) in enumerate(zip(source_clauses, conditions)):
        candidate = str(cond.get("text", "") or "")
        clause_issues = _constraint_issues(source, candidate)
        if not clause_issues:
            continue
        for issue in clause_issues:
            issues.append({"condition_index": index, **issue})
        cond["text"] = source
        cond["keyword"] = fallback_keyword_fn(source) if fallback_keyword_fn else source
        cond["is_numeric"] = is_executable_numeric_condition(source)
        changed = True

    if changed:
        repaired["conditions"] = conditions
        _append_source(repaired, "constraint_preserve")
    validation = repaired.setdefault("ir_validation", {})
    validation["valid"] = not bool(validation.get("issues"))
    return repaired


def validate_and_repair_analysis(
    analysis: dict,
    original_condition: str,
    fallback_keyword_fn: Optional[Callable[[str], str]] = None,
) -> dict:
    """Apply deterministic IR repairs that should always run after analysis."""
    repaired = enforce_compound_structure(
        analysis,
        original_condition,
        fallback_keyword_fn=fallback_keyword_fn,
    )
    repaired = preserve_literal_clause_texts(
        repaired,
        original_condition,
        fallback_keyword_fn=fallback_keyword_fn,
    )
    repaired = preserve_single_temporal_condition(
        repaired,
        original_condition,
        fallback_keyword_fn=fallback_keyword_fn,
    )
    repaired = validate_clause_constraints(
        repaired,
        original_condition,
        fallback_keyword_fn=fallback_keyword_fn,
    )
    validation = repaired.setdefault("ir_validation", {})
    validation.setdefault("issues", [])
    validation["valid"] = not bool(validation.get("issues"))
    return repaired


def _append_source(analysis: dict, tag: str) -> None:
    source = analysis.get("source", "unknown")
    if tag not in str(source):
        analysis["source"] = f"{source}+{tag}"
