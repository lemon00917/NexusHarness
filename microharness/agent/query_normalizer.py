"""
Query normalization for medical filtering.

The normalizer cleans user input before routing/execution. The LLM may repair
ambiguous natural language, but deterministic code validates and normalizes
operators/numbers so final matching does not depend on model arithmetic.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class QueryNormalization:
    original: str
    normalized: str
    corrections: list[str] = field(default_factory=list)
    confidence: float = 1.0
    source: str = "deterministic"
    needs_review: bool = False

    def to_dict(self) -> dict:
        return {
            "原始问题": self.original,
            "规范问题": self.normalized,
            "修正项": self.corrections,
            "置信度": self.confidence,
            "来源": self.source,
            "是否需要复核": self.needs_review,
        }


_OPERATOR_MAP = {
    "》": ">",
    "＞": ">",
    "﹥": ">",
    "〉": ">",
    "大於": "大于",
    "高於": "高于",
    "《": "<",
    "＜": "<",
    "﹤": "<",
    "〈": "<",
    "小於": "小于",
    "低於": "低于",
    "＝": "=",
    "≧": ">=",
    "≦": "<=",
    "≥": ">=",
    "≤": "<=",
    "=>": ">=",
    "=<": "<=",
    "×": "x",
    "X": "x",
}


def normalize_query_text(text: str) -> tuple[str, list[str]]:
    """Deterministically normalize symbols, spacing, and common typos."""
    original = text or ""
    normalized = original.strip()
    corrections: list[str] = []

    if not normalized:
        return normalized, corrections

    before = normalized
    normalized = normalized.translate(str.maketrans({
        "０": "0", "１": "1", "２": "2", "３": "3", "４": "4",
        "５": "5", "６": "6", "７": "7", "８": "8", "９": "9",
    }))
    if normalized != before:
        corrections.append("全角数字转半角")

    for src, dst in _OPERATOR_MAP.items():
        if src in normalized:
            normalized = normalized.replace(src, dst)
            corrections.append(f"{src} -> {dst}")

    before = normalized
    normalized = re.sub(r"\s+", "", normalized)
    if normalized != before:
        corrections.append("移除多余空白")

    # Normalize scientific notation variants: 15 x 10 ^ 9 -> 15x10^9.
    before = normalized
    normalized = re.sub(r"(\d+(?:\.\d+)?)\s*[x*]\s*10\s*\^\s*([+-]?\d+)", r"\1x10^\2", normalized)
    if normalized != before:
        corrections.append("科学计数法格式归一")

    return normalized, corrections


def _extract_json_obj(text: str) -> dict[str, Any]:
    cleaned = (text or "").strip()
    for fence in ("```json", "```"):
        if fence in cleaned:
            parts = cleaned.split(fence)
            if len(parts) >= 2:
                cleaned = parts[1].split("```")[0] if "```" in parts[1] else parts[1]
                cleaned = cleaned.strip()
                break
    try:
        obj = json.loads(cleaned)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        pass
    m = re.search(r"\{.*\}", cleaned, re.S)
    if m:
        try:
            obj = json.loads(m.group(0))
            return obj if isinstance(obj, dict) else {}
        except Exception:
            return {}
    return {}


_CMP_OPERATOR_KIND = {
    ">": "gt",
    "大于": "gt",
    "高于": "gt",
    "多于": "gt",
    "超过": "gt",
    "<": "lt",
    "小于": "lt",
    "少于": "lt",
    "低于": "lt",
    ">=": "gte",
    "不低于": "gte",
    "不少于": "gte",
    "至少": "gte",
    "以上": "gte",
    "及以上": "gte",
    "<=": "lte",
    "不超过": "lte",
    "至多": "lte",
    "以下": "lte",
    "及以下": "lte",
    "=": "eq",
    "等于": "eq",
}


def _normalize_comparison_operator(operator: str) -> str:
    return _CMP_OPERATOR_KIND.get(operator, operator)


def _comparison_signatures(text: str) -> list[tuple[str, float]]:
    """Extract generic comparison signatures without medical vocabulary.

    The LLM normalizer is allowed to repair wording, but it must not change
    comparison direction or threshold. Time-window phrases such as "术前48小时内"
    are intentionally ignored here; those are validated by the time-window
    checks below.
    """
    from microharness.medical.temporal_parser import normalize_numeric_text, parse_cn_number

    normalized = normalize_numeric_text(text or "")
    num_pat = (
        r"(?:\d+(?:\.\d+)?"
        r"(?:\s*(?:x|\*)\s*10(?:\s*\^\s*[+-]?\d+|[⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻]+))?"
        r"|[零〇一二两三四五六七八九十百千万亿半]+)"
    )
    prefix_op_pat = (
        r"(>=|<=|>|<|=|大于|小于|高于|低于|多于|少于|超过|"
        r"不超过|不低于|不少于|至少|至多|等于)"
    )
    postfix_op_pat = r"(以上|及以上|以下|及以下)"

    signatures: list[tuple[str, float]] = []
    for match in re.finditer(prefix_op_pat + r"\s*(" + num_pat + r")", normalized):
        value = parse_cn_number(match.group(2))
        if value is not None:
            signatures.append((_normalize_comparison_operator(match.group(1)), float(value)))

    for match in re.finditer(r"(" + num_pat + r")\s*(?:/[A-Za-z一-龥0-9^⁰¹²³⁴⁵⁶⁷⁸⁹]+)?\s*" + postfix_op_pat, normalized):
        value = parse_cn_number(match.group(1))
        if value is not None:
            signatures.append((_normalize_comparison_operator(match.group(2)), float(value)))

    return signatures


def _comparison_signatures_preserved(deterministic: str, candidate: str) -> bool:
    det_signatures = _comparison_signatures(deterministic)
    cand_signatures = _comparison_signatures(candidate)
    if not det_signatures and not cand_signatures:
        return True
    if len(det_signatures) != len(cand_signatures):
        return False
    for (det_op, det_value), (cand_op, cand_value) in zip(det_signatures, cand_signatures):
        if det_op != cand_op:
            return False
        tolerance = max(1e-9, abs(det_value) * 1e-9)
        if abs(det_value - cand_value) > tolerance:
            return False
    return True


def _safe_accept_llm(original: str, deterministic: str, candidate: str, confidence: float) -> bool:
    if not candidate or not isinstance(candidate, str):
        return False
    candidate = candidate.strip()
    if len(candidate) > max(80, len(original) * 3):
        return False
    if confidence < 0.45:
        return False
    # The LLM may add omitted verbs, but it should not produce an unrelated
    # query. Require some character overlap with the deterministic text.
    base_chars = {c for c in deterministic if "\u4e00" <= c <= "\u9fff" or c.isalnum()}
    cand_chars = {c for c in candidate if "\u4e00" <= c <= "\u9fff" or c.isalnum()}
    if base_chars and len(base_chars & cand_chars) / max(1, len(base_chars)) < 0.35:
        return False
    if "患者" not in deterministic and "患者" in candidate:
        return False
    action_groups = [
        ("开了", "开具", "开药"),
        ("服用", "服用了", "服用过", "口服"),
        ("使用", "使用过", "用了", "用过"),
        ("注射", "注射过", "注射了"),
        ("诊断", "诊断为", "确诊"),
        ("检查", "做了", "做过"),
        ("输血", "输过血"),
    ]
    for group in action_groups:
        det_hit = any(token in deterministic for token in group)
        cand_hit = any(token in candidate for token in group)
        if det_hit != cand_hit:
            return False
        if det_hit and cand_hit:
            det_group = next((i for i, g in enumerate(action_groups) if any(t in deterministic for t in g)), None)
            cand_group = next((i for i, g in enumerate(action_groups) if any(t in candidate for t in g)), None)
            if det_group != cand_group:
                return False
    cmp_pat = r"(>=|<=|>|<|=|大于|小于|高于|低于|超过|不超过|不低于|不少于|至少|至多|以上|以下)"
    if re.search(cmp_pat, candidate) and not re.search(cmp_pat, deterministic):
        return False
    if len(re.findall(cmp_pat, candidate)) > len(re.findall(cmp_pat, deterministic)):
        return False
    if not _comparison_signatures_preserved(deterministic, candidate):
        return False
    qualitative_markers = ("偏高", "偏低", "升高", "降低", "增高", "减少", "异常", "不正常", "正常")
    if any(marker in deterministic for marker in qualitative_markers):
        if not any(marker in candidate for marker in qualitative_markers):
            return False
        for marker in qualitative_markers:
            if marker in deterministic and marker not in candidate:
                return False
        if re.search(r"[<>]=?\s*(?:正常|异常)", candidate) and not re.search(r"[<>]=?\s*(?:正常|异常)", deterministic):
            return False
    scientific_num_pat = r"\d+(?:\.\d+)?\s*[x*]\s*10(?:\^?\d+|[⁰¹²³⁴⁵⁶⁷⁸⁹]+)"
    if re.search(scientific_num_pat, candidate) and not re.search(scientific_num_pat, deterministic):
        return False
    if "正常值" in candidate and "正常值" not in deterministic:
        return False
    if re.search(r"/[A-Za-z%\u4e00-\u9fff]+", deterministic) and not re.search(r"/[A-Za-z%\u4e00-\u9fff]+", candidate):
        return False
    time_window_added = re.search(
        r"(前|后)\s*\d+(?:\.\d+)?\s*(?:分钟|小时|天|日|周|月)内",
        candidate,
    )
    if time_window_added and not re.search(
        r"(前|后)\s*\d+(?:\.\d+)?\s*(?:分钟|小时|天|日|周|月)内",
        deterministic,
    ):
        return False
    same_day_pat = r"(?:术前|术后|手术前|手术后|入院前|入院后|出院前|出院后)?(?:当天|当日)"
    if re.search(same_day_pat, deterministic) and not re.search(same_day_pat, candidate):
        return False
    if re.search(r"(?:术前|术后|手术前|手术后|入院前|入院后|出院前|出院后)(?:1|一)天", candidate) and re.search(same_day_pat, deterministic):
        return False
    return True


def normalize_query(condition: str, model: str = "qwen2.5:3b", timeout: int = 45) -> QueryNormalization:
    """Normalize a raw user query before routing.

    Deterministic normalization always runs. LLM normalization is advisory and
    accepted only after validation.
    """
    deterministic, corrections = normalize_query_text(condition)
    result = QueryNormalization(
        original=condition,
        normalized=deterministic,
        corrections=list(corrections),
        confidence=1.0,
        source="deterministic",
        needs_review=False,
    )

    if not deterministic:
        return result

    try:
        from microharness.ollama import OllamaClient
        from microharness.ollama.model_profile import get_profile
        from microharness.ollama.prompt_adapter import build_query_normalization_prompt

        profile = get_profile(model)
        prompt = build_query_normalization_prompt(profile, condition, deterministic)
        client = OllamaClient(
            model=model,
            timeout=timeout,
            format_json=True,
            num_predict=1024,
        )
        resp = client.chat([{"role": "user", "content": prompt}], temperature=0.0)
        parsed = _extract_json_obj(resp)
        candidate = str(parsed.get("normalized_condition", "") or parsed.get("规范问题", "")).strip()
        confidence = float(parsed.get("confidence", parsed.get("置信度", 0.0)) or 0.0)
        llm_corrections = parsed.get("corrections", parsed.get("修正项", [])) or []
        if isinstance(llm_corrections, str):
            llm_corrections = [llm_corrections]

        candidate_norm, candidate_corrections = normalize_query_text(candidate)
        if _safe_accept_llm(condition, deterministic, candidate_norm, confidence):
            result.normalized = candidate_norm
            result.corrections = list(dict.fromkeys(corrections + list(llm_corrections) + candidate_corrections))
            result.confidence = confidence
            result.source = "llm+deterministic"
            result.needs_review = bool(parsed.get("needs_review", parsed.get("是否需要复核", False)))
        else:
            result.corrections.append("LLM归一化未通过校验，使用确定性归一化")
            result.confidence = min(result.confidence, max(confidence, 0.5))
            result.needs_review = True
    except Exception as exc:
        result.corrections.append(f"LLM归一化失败: {str(exc)[:80]}")
        result.needs_review = False

    return result
