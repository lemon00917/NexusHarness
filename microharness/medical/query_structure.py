"""Structural repair for medical filter query analysis.

This module owns grammar-level repairs after LLM understanding:
compound splitting, structural clauses such as age/history, and filtering
non-executable filler fragments. The rules describe reusable query shapes and
do not encode patient-specific or example-specific answers.
"""

from __future__ import annotations

import re
from typing import Callable, Optional

from microharness.medical.query_ir_validator import (
    is_executable_numeric_condition,
    validate_and_repair_analysis,
)
from microharness.medical.semantic_rules import (
    augment_analysis_routes,
    maybe_split_compound_analysis,
)
from microharness.medical.temporal_parser import parse_cn_number


KeywordFn = Optional[Callable[[str], str]]
NumericFn = Optional[Callable[[str], bool]]


def is_non_executable_subcondition(text: str) -> bool:
    """Return True for context/filler fragments that cannot be judged alone."""
    cleaned = re.sub(r"[\s　,，;；、。]+", "", str(text or ""))
    cleaned = re.sub(r"^(的|为|是|有|在)+", "", cleaned)
    cleaned = re.sub(r"(的患者|的病人|的病例|患者|病人|病例|的人)$", "", cleaned)
    if not cleaned:
        return True
    if cleaned in {
        "患者", "病人", "病例", "住院", "住院期间", "住院期内",
        "入院期间", "治疗期间", "就诊期间", "本次住院",
    }:
        return True
    return bool(re.fullmatch(r"(入院|出院|手术|术前|术后|住院)(时|前|后|中|期间|期内)?", cleaned))


def augment_structural_conditions(
    analysis: dict,
    original_condition: str,
    fallback_keyword_fn: KeywordFn = None,
    executable_numeric_fn: NumericFn = None,
) -> dict:
    """Recover structured clauses that small LLMs often drop.

    Currently covers generic age comparisons and disease-history duration
    phrases. It keeps the entity-specific parts supplied by the user.
    """
    analysis = analysis or {}
    original_conditions = list(analysis.get("conditions", []) or [])
    conditions = list(original_conditions)
    extracted_spans: list[tuple[int, int]] = []
    added_texts: set[str] = set()
    structural_changed = False
    keyword_fn = fallback_keyword_fn or (lambda text: text)
    numeric_fn = executable_numeric_fn or is_executable_numeric_condition

    def add_condition(cond: dict, span: tuple[int, int] | None = None) -> None:
        text = cond.get("text", "")
        if text and not any(c.get("text") == text for c in conditions):
            conditions.append(cond)
            added_texts.add(text)
            if span:
                extracted_spans.append(span)

    num_pat = r"(\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百千万亿]+)"
    suffix_pat = r"(?:的患者|的病人|的病例|患者|病人|病例)?"

    def _clean_residual_piece(text: str) -> str:
        cleaned = str(text or "")
        cleaned = re.sub(r"(的患者|的病人|的病例|患者|病人|病例)$", "", cleaned)
        cleaned = re.sub(r"[\s　，,；;。、“”\"'（）()]+", "", cleaned)
        cleaned = re.sub(r"^(并且|而且|同时|且|以及|和)+", "", cleaned)
        cleaned = re.sub(r"(并且|而且|同时|且|以及|和)+$", "", cleaned)
        return cleaned

    def _split_residual(text: str) -> list[str]:
        pieces = [
            _clean_residual_piece(p)
            for p in re.split(r"\s*(?:并且|而且|同时|且|以及|和|，|,|；|;)\s*", str(text or ""))
        ]
        return [p for p in pieces if p and not is_non_executable_subcondition(p)]

    def _age_condition_from_match(match, op: str) -> dict | None:
        raw_num = next((g for g in match.groups() if g and re.fullmatch(num_pat, g)), "")
        val = parse_cn_number(raw_num)
        if val is None:
            return None
        val_display = int(val) if float(val).is_integer() else val
        return {
            "text": f"年龄{op}{val_display}岁",
            "keyword": "年龄",
            "modifiers": [],
            "is_numeric": True,
            "target_docs": ["入院记录", "出院记录", "门急诊病历", "手术记录", "首次病程记录", "日常病程记录"],
            "target_sections": ["年龄"],
            "target_skills": [],
        }

    age_patterns = [
        (rf"{num_pat}\s*岁\s*(以上|及以上)", ">="),
        (rf"{num_pat}\s*岁\s*(以下|及以下)", "<="),
        (rf"(?:年龄)?\s*(大于|超过|高于)\s*{num_pat}\s*岁", ">"),
        (rf"(?:年龄)?\s*(小于|低于|少于)\s*{num_pat}\s*岁", "<"),
        (rf"(?:年龄)?\s*(不低于|不少于|至少)\s*{num_pat}\s*岁", ">="),
        (rf"(?:年龄)?\s*(不超过|至多)\s*{num_pat}\s*岁", "<="),
    ]

    for cond in conditions:
        text = str(cond.get("text", ""))
        if text.startswith("年龄"):
            continue
        for pattern, op in age_patterns:
            match = re.fullmatch(pattern, text)
            if not match:
                continue
            age_cond = _age_condition_from_match(match, op)
            if age_cond:
                cond.update(age_cond)
                structural_changed = True
            break

    if not any(str(c.get("text", "")).startswith("年龄") for c in conditions):
        for pattern, op in age_patterns:
            match = re.search(pattern, original_condition)
            if not match:
                continue
            age_cond = _age_condition_from_match(match, op)
            if age_cond:
                add_condition(age_cond, match.span())
            break

    history_pattern = rf"(?:有|患有|存在)?\s*{num_pat}\s*年\s*(?:以上|及以上|余|多)?\s*([^，,；;。]+?)(?:病史|史)"
    for match in re.finditer(history_pattern, original_condition):
        raw_num = match.group(1)
        disease = re.sub(r"^(的|有|患有|存在)", "", match.group(2)).strip()
        disease = re.sub(r"(疾病|患者)$", "", disease).strip()
        val = parse_cn_number(raw_num)
        if not disease or val is None:
            continue
        val_display = int(val) if float(val).is_integer() else val
        add_condition({
            "text": f"{disease}病史>={val_display}年",
            "keyword": disease,
            "modifiers": [f"病史>={val_display}年"],
            "is_numeric": False,
            "target_docs": ["入院记录", "门急诊病历"],
            "target_sections": ["既往史", "现病史"],
            "target_skills": ["diagnosis-query"],
        }, match.span())

    plain_history_pattern = rf"(?:既往)?(?:有|患有|存在|诊断为|确诊为|得过|有过)?\s*([^，,；;。]+?)(?:病史|史){suffix_pat}"
    for match in re.finditer(plain_history_pattern, original_condition):
        if any(not (match.end() <= start or match.start() >= end) for start, end in extracted_spans):
            continue
        raw_disease = match.group(1)
        raw_disease = re.sub(
            r"(?:年龄)?\s*(?:大于|超过|高于|小于|低于|少于|不低于|不少于|至少|不超过|至多)\s*" + num_pat + r"\s*岁",
            "",
            raw_disease,
        )
        raw_disease = re.sub(num_pat + r"\s*岁\s*(?:以上|及以上|以下|及以下)", "", raw_disease)
        raw_disease = re.sub(num_pat + r"\s*年\s*(?:以上|及以上|余|多)?", "", raw_disease)
        disease = re.sub(r"^(的|有|患有|存在|既往|并且|而且|同时|且|以及|和)", "", raw_disease).strip()
        disease = re.sub(r"(疾病|患者|病人|病例)$", "", disease).strip()
        if not disease:
            continue
        add_condition({
            "text": f"{disease}病史",
            "keyword": disease,
            "modifiers": ["病史"],
            "is_numeric": False,
            "target_docs": ["入院记录", "门急诊病历"],
            "target_sections": ["既往史", "现病史"],
            "target_skills": ["diagnosis-query"],
        }, match.span())

    def _strip_structural_fragments(text: str) -> str:
        residual = str(text or "")
        residual = re.sub(
            r"(?:年龄)?\s*(?:大于|超过|高于|小于|低于|少于|不低于|不少于|至少|不超过|至多)\s*" + num_pat + r"\s*岁",
            "",
            residual,
        )
        residual = re.sub(num_pat + r"\s*岁\s*(?:以上|及以上|以下|及以下)", "", residual)
        residual = re.sub(history_pattern, "", residual)
        residual = re.sub(plain_history_pattern, "", residual)
        residual = re.sub(r"(的患者|的病人|的病例|患者|病人|病例)$", "", residual)
        residual = re.sub(r"^[\s　、“”\"'（）()]+|[\s　、“”\"'（）()]+$", "", residual)
        residual = re.sub(r"^(并且|而且|同时|且|以及|和)+", "", residual)
        residual = re.sub(r"(并且|而且|同时|且|以及|和)+$", "", residual)
        return residual

    if added_texts or structural_changed:
        for piece in _split_residual(_strip_structural_fragments(original_condition)):
            add_condition({
                "text": piece,
                "keyword": keyword_fn(piece),
                "modifiers": [],
                "is_numeric": numeric_fn(piece),
                "target_docs": [],
                "target_sections": [],
                "target_skills": [],
            })

        normalized_conditions = []
        for cond in conditions:
            text = str(cond.get("text", ""))
            if text in added_texts:
                normalized_conditions.append(cond)
                continue
            residual_pieces = _split_residual(_strip_structural_fragments(text))
            has_structural_piece = bool(
                re.search(r"\d+\s*岁\s*(?:以上|及以上|以下|及以下)", text)
                or re.search(r"\d+\s*年\s*(?:以上|及以上|余|多)?.{1,20}(?:病史|史)", text)
                or re.search(r"(?:病史|史)", text)
            )
            if has_structural_piece or len(residual_pieces) > 1:
                if not residual_pieces:
                    continue
                for piece in residual_pieces:
                    if piece not in added_texts and not any(c.get("text") == piece for c in normalized_conditions):
                        normalized_conditions.append({
                            **cond,
                            "text": piece,
                            "keyword": keyword_fn(piece),
                            "modifiers": [],
                            "is_numeric": numeric_fn(piece),
                            "target_docs": [],
                            "target_sections": [],
                            "target_skills": [],
                        })
                continue
            normalized_conditions.append(cond)
        conditions = normalized_conditions

    if len(conditions) != len(original_conditions):
        analysis["conditions"] = conditions
        if len(conditions) > 1 and not analysis.get("connector"):
            analysis["connector"] = "and"
            analysis["type"] = "compound"
        source = analysis.get("source", "unknown")
        if "structural_augment" not in source:
            analysis["source"] = f"{source}+structural_augment"
    return analysis


def dedupe_equivalent_conditions(analysis: dict, fallback_keyword_fn: KeywordFn = None) -> dict:
    """Remove duplicated clauses that differ only by Chinese function words."""
    if not isinstance(analysis, dict):
        return analysis
    conditions = list(analysis.get("conditions", []) or [])
    if len(conditions) <= 1:
        return analysis

    def canonical(cond: dict) -> tuple:
        text = str(cond.get("text", "") or "")
        keyword = ""
        entity = str(cond.get("entity", "") or "").strip()
        if fallback_keyword_fn:
            keyword = fallback_keyword_fn(text)
        if entity:
            keyword = entity
        elif not keyword:
            keyword = text
        normalized = re.sub(r"(的患者|的病人|的病例|患者|病人|病例)$", "", keyword)
        normalized = re.sub(r"^(有诊断为|有诊断有|有诊断|诊断为|诊断有|确诊为|确诊有|患有|存在|有过|有)", "", normalized)
        normalized = re.sub(r"[\s　，,；;。、“”\"'（）()]+", "", normalized)
        semantic = str(cond.get("semantic_class", "") or "")
        modifiers = tuple(sorted(str(m) for m in (cond.get("modifiers") or [])))
        is_numeric = bool(cond.get("is_numeric"))
        return (normalized, semantic, modifiers, is_numeric)

    deduped = []
    seen = set()
    changed = False
    for cond in conditions:
        key = canonical(cond)
        if key in seen:
            changed = True
            continue
        seen.add(key)
        deduped.append(cond)
    if changed:
        analysis["conditions"] = deduped
        source = analysis.get("source", "unknown")
        if "dedupe" not in source:
            analysis["source"] = f"{source}+dedupe"
    return analysis


def normalize_condition_entities(analysis: dict, fallback_keyword_fn: KeywordFn = None) -> dict:
    """Keep keyword/entity aligned with the user's core concept."""
    if not isinstance(analysis, dict) or not fallback_keyword_fn:
        return analysis
    changed = False
    for cond in analysis.get("conditions", []) or []:
        if not isinstance(cond, dict):
            continue
        text = str(cond.get("text", "") or "")
        kw = fallback_keyword_fn(text)
        if not kw:
            continue
        old_keyword = str(cond.get("keyword", "") or "")
        old_entity = str(cond.get("entity", "") or "")
        if not old_keyword or old_keyword == text or len(kw) < len(old_keyword):
            cond["keyword"] = kw
            changed = True
        if not old_entity or old_entity == text or len(kw) < len(old_entity):
            cond["entity"] = kw
            changed = True
    if changed:
        source = analysis.get("source", "unknown")
        if "entity_normalize" not in source:
            analysis["source"] = f"{source}+entity_normalize"
    return analysis


def repair_analysis_structure(
    analysis: dict,
    original_condition: str,
    fallback_keyword_fn: KeywordFn = None,
) -> dict:
    """Apply deterministic structure repairs in the canonical order."""
    repaired = maybe_split_compound_analysis(
        analysis,
        original_condition,
        fallback_keyword_fn=fallback_keyword_fn,
    )
    repaired = augment_structural_conditions(
        repaired,
        original_condition,
        fallback_keyword_fn=fallback_keyword_fn,
    )
    repaired = augment_analysis_routes(
        repaired,
        original_condition,
        fallback_keyword_fn=fallback_keyword_fn,
    )
    repaired = normalize_condition_entities(
        repaired,
        fallback_keyword_fn=fallback_keyword_fn,
    )
    repaired = dedupe_equivalent_conditions(
        repaired,
        fallback_keyword_fn=fallback_keyword_fn,
    )
    repaired = validate_and_repair_analysis(
        repaired,
        original_condition,
        fallback_keyword_fn=fallback_keyword_fn,
    )
    return repaired
