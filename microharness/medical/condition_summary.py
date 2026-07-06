"""
Condition summary utilities for logs and UI display.

This module does not decide whether a patient matches a condition. It only
builds a compact explanation of how a natural-language condition is shaped:
subject, qualifier, and judgment. Execution still happens in router, scheduler,
service, and domain-rule modules.
"""

from __future__ import annotations

import re
from typing import Callable, Optional

from microharness.medical.temporal_parser import operator_display, parse_numeric_comparison


# Generic Chinese clinical-query grammar. These are display categories, not
# patient-specific or example-specific rules.
_CN_NUM = r"零〇一二两三四五六七八九十百千万亿\d."
_TIME_UNIT = r"分钟|小时|天|日|周|月|个月"
_EVENT_ANCHOR = r"术|手术|入院|出院|住院|治疗|就诊|复查"

_QUALIFIER_PATTERNS = (
    r"(?:住院|治疗|就诊|复查)(?:期间|期内|时)",
    r"(?:本次)?住院",
    r"(?:入院|出院|手术|术)(?:前|后|时|中|期间|期内)",
    rf"(?:{_EVENT_ANCHOR})(?:前|后)?\s*[{_CN_NUM}]+\s*(?:{_TIME_UNIT})(?:内|前|后)?",
    rf"(?:前|后)\s*[{_CN_NUM}]+\s*(?:{_TIME_UNIT})",
)

_STATUS_PATTERNS = (
    r"高于参考范围|低于参考范围|偏高|升高|增高|偏低|降低|减少|异常|正常|阳性|阴性",
    r"没有(?:明显)?好转|未(?:见)?(?:明显)?好转|无(?:明显)?好转|好转|缓解|改善|治愈|加重|恶化",
)

_ACTION_PATTERNS = (
    r"诊断为|确诊为|诊断",
    r"开了|开过|开具|服用了|服用过|使用了|使用过|注射了|注射过|用了|用过",
    r"做了|做过|检查|检测|输过|输注|输血",
)


def extract_condition_qualifiers(text: str) -> list[str]:
    """Extract time/phase qualifiers for display."""
    text = str(text or "")
    found: list[str] = []
    for pattern in _QUALIFIER_PATTERNS:
        for match in re.finditer(pattern, text):
            value = match.group(0).strip()
            if not value:
                continue
            if any(value in existing for existing in found):
                continue
            found = [existing for existing in found if existing not in value]
            if value not in found:
                found.append(value)
    return found


def extract_condition_judgment(text: str) -> str:
    """Extract comparison/status/action words for display."""
    text = str(text or "")
    cmp_info = parse_numeric_comparison(text)
    if cmp_info:
        return f"{operator_display(cmp_info.operator)}{cmp_info.threshold:g}{cmp_info.unit or ''}"

    for pattern in _STATUS_PATTERNS + _ACTION_PATTERNS:
        match = re.search(pattern, text)
        if match:
            return match.group(0)
    return "存在/匹配"


def summarize_condition_structure(
    text: str,
    cond: Optional[dict] = None,
    fallback_keyword_fn: Optional[Callable[[str], str]] = None,
) -> dict[str, str]:
    """Build a compact condition summary for logs.

    Args:
        text: The condition text to summarize.
        cond: Optional query-understanding condition dict.
        fallback_keyword_fn: Optional project keyword extractor. Passing this in
            keeps this module independent from web-layer helpers.
    """
    cond = cond or {}
    raw_keyword = cond.get("keyword") or ""
    keyword = ""
    if fallback_keyword_fn:
        keyword = fallback_keyword_fn(str(raw_keyword or text))
    if not keyword:
        keyword = str(raw_keyword or "").strip()
    if not keyword and fallback_keyword_fn:
        keyword = fallback_keyword_fn(str(text or ""))

    qualifiers = extract_condition_qualifiers(text)
    return {
        "主体": keyword or "未识别",
        "限定": "、".join(qualifiers) if qualifiers else "无",
        "判断": extract_condition_judgment(text),
    }
