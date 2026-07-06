"""
Medical temporal and numeric parsing utilities.

This module handles language grammar such as Chinese numerals, comparison
operators, and duration units. It intentionally does not contain disease,
drug, or patient-specific knowledge.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Optional


@dataclass
class NumericComparison:
    subject: str
    operator: str
    threshold: float
    unit: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


_SUPERSCRIPT_DIGITS = str.maketrans({
    "⁰": "0",
    "¹": "1",
    "²": "2",
    "³": "3",
    "⁴": "4",
    "⁵": "5",
    "⁶": "6",
    "⁷": "7",
    "⁸": "8",
    "⁹": "9",
    "⁺": "+",
    "⁻": "-",
})


def normalize_numeric_text(text: str) -> str:
    """Normalize common numeric symbols without changing medical meaning."""
    text = (text or "").strip()
    text = (
        text.replace("＞", ">")
        .replace("》", ">")
        .replace("﹥", ">")
        .replace("〉", ">")
        .replace("＜", "<")
        .replace("《", "<")
        .replace("﹤", "<")
        .replace("〈", "<")
        .replace("≥", ">=")
        .replace("≤", "<=")
        .replace("≧", ">=")
        .replace("≦", "<=")
        .replace("×", "x")
        .replace("X", "x")
    )
    text = re.sub(r"10\s*[~～]\s*([+-]?\d+)", r"10^\1", text)
    text = re.sub(r"10\s*\*\*\s*([+-]?\d+)", r"10^\1", text)
    return text


def parse_scientific_number(raw: str) -> Optional[float]:
    """Parse forms like 15x10^9, 15*10⁹, 1.5x10^10."""
    raw = normalize_numeric_text(raw or "")
    raw = re.sub(r"[\s,，]", "", raw)
    if not raw:
        return None

    superscript_match = re.fullmatch(
        r"([+-]?\d+(?:\.\d+)?)(?:x|\*)10([⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻]+)",
        raw,
    )
    if superscript_match:
        base = float(superscript_match.group(1))
        exponent = int(superscript_match.group(2).translate(_SUPERSCRIPT_DIGITS))
        return base * (10 ** exponent)

    caret_match = re.fullmatch(
        r"([+-]?\d+(?:\.\d+)?)(?:x|\*)10\^([+-]?\d+)",
        raw,
    )
    if caret_match:
        base = float(caret_match.group(1))
        exponent = int(caret_match.group(2))
        return base * (10 ** exponent)

    plain_match = re.fullmatch(r"[+-]?\d+(?:\.\d+)?", raw)
    if plain_match:
        return float(raw)

    return None


def parse_unit_multiplier(unit: str) -> float:
    """Return multiplier encoded in units such as *10^9/L or x10⁹/L."""
    unit = normalize_numeric_text(unit or "")
    unit = re.sub(r"\s+", "", unit)
    if not unit:
        return 1.0

    superscript_match = re.search(r"(?:x|\*)?10([⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻]+)", unit)
    if superscript_match:
        exponent = int(superscript_match.group(1).translate(_SUPERSCRIPT_DIGITS))
        return float(10 ** exponent)

    caret_match = re.search(r"(?:x|\*)?10\^([+-]?\d+)", unit)
    if caret_match:
        exponent = int(caret_match.group(1))
        return float(10 ** exponent)

    return 1.0


def parse_measurement_value(value: str, unit: str = "") -> Optional[float]:
    """Parse a displayed result value with an optional scientific unit."""
    parsed = parse_scientific_number(value)
    if parsed is None:
        return None
    if re.search(r"(?:x|\*)\s*10(?:\^|[⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻])", normalize_numeric_text(value or "")):
        return parsed
    return parsed * parse_unit_multiplier(unit)


def parse_cn_number(raw: str) -> Optional[float]:
    """Parse Arabic or Chinese numerals without relying on fixed phrases."""
    raw = (raw or "").strip().replace("个", "")
    if not raw:
        return None
    scientific = parse_scientific_number(raw)
    if scientific is not None:
        return scientific
    if raw == "半":
        return 0.5
    if raw in ("一半", "半个"):
        return 0.5
    if re.fullmatch(r"\d+(?:\.\d+)?", raw):
        return float(raw)
    try:
        import cn2an

        return float(cn2an.cn2an(raw, "smart"))
    except Exception:
        pass

    digits = {
        "零": 0,
        "〇": 0,
        "一": 1,
        "二": 2,
        "两": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
    }
    units = {"十": 10, "百": 100, "千": 1000, "万": 10000, "亿": 100000000}
    total = 0
    section = 0
    number = 0
    for ch in raw:
        if ch in digits:
            number = digits[ch]
        elif ch in units:
            unit = units[ch]
            if unit >= 10000:
                section = (section + number) * unit
                total += section
                section = 0
            else:
                section += (number or 1) * unit
            number = 0
        else:
            return None
    return float(total + section + number)


def normalize_time_unit(unit: str) -> str:
    unit = unit or ""
    if unit == "日":
        return "天"
    if unit == "个月":
        return "月"
    if unit in ("周", "星期"):
        return "周"
    if unit == "钟头":
        return "小时"
    if unit == "分":
        return "分钟"
    return unit


def convert_numeric_unit(value: float, from_unit: str, to_unit: str) -> float:
    """Convert common duration units for comparing precomputed values."""
    from_unit = normalize_time_unit(from_unit)
    to_unit = normalize_time_unit(to_unit)
    if not from_unit or not to_unit or from_unit == to_unit:
        return value
    seconds = {
        "分钟": 60,
        "小时": 3600,
        "天": 86400,
        "周": 7 * 86400,
        "月": 30 * 86400,
    }
    if from_unit in seconds and to_unit in seconds:
        return value * seconds[from_unit] / seconds[to_unit]
    return value


def parse_numeric_comparison(condition: str) -> Optional[NumericComparison]:
    """Extract a generic numeric comparison from Chinese natural language."""
    condition = normalize_numeric_text(condition or "")
    num_pat = r"((?:\d+(?:\.\d+)?(?:\s*(?:x|\*)\s*10(?:\s*\^\s*[+-]?\d+|[⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻]+))?)|[零〇一二两三四五六七八九十百千万亿半]+)"
    unit_pat = r"(天|日|年|月|个月|周|星期|小时|钟头|分钟|分|岁|个|次|度|%)?"
    cmp_pat = (
        r"(.+?)\s*"
        r"(小于|少于|低于|大于|多于|超过|不超过|不低于|不少于|至少|至多|等于|>=|<=|>|<|=|≥|≤|以上|以下)"
        r"\s*" + num_pat + r"\s*(个)?\s*" + unit_pat
    )
    m = re.search(cmp_pat, condition)
    if m:
        value = parse_cn_number(m.group(3))
        if value is not None:
            return NumericComparison(
                subject=m.group(1).strip(),
                operator=m.group(2),
                threshold=value,
                unit=normalize_time_unit(m.group(5) or ""),
            )

    # Postfix comparison grammar: "40岁以上", "10年以上", "白细胞计数15x10^9/L以上".
    # This is language grammar, not a medical term list.
    postfix_pat = (
        r"(.{0,40}?)\s*"
        + num_pat
        + r"\s*(个)?\s*"
        + unit_pat
        + r"(?:/[A-Za-z一-龥0-9^⁰¹²³⁴⁵⁶⁷⁸⁹]+)?\s*"
        + r"(以上|及以上|以下|及以下|以内|内)"
    )
    m = re.search(postfix_pat, condition)
    if not m:
        return None
    value = parse_cn_number(m.group(2))
    if value is None:
        return None
    operator = {
        "及以上": "以上",
        "及以下": "以下",
        "以内": "以下",
        "内": "以下",
    }.get(m.group(5), m.group(5))
    subject = (m.group(1) or "").strip()
    unit = normalize_time_unit(m.group(4) or "")
    if not subject and unit == "岁":
        subject = "年龄"
    return NumericComparison(
        subject=subject,
        operator=operator,
        threshold=value,
        unit=unit,
    )


def operator_display(operator: str) -> str:
    return {
        "小于": "<",
        "少于": "<",
        "低于": "<",
        "大于": ">",
        "多于": ">",
        "超过": ">",
        "不超过": "≤",
        "至多": "≤",
        "以下": "≤",
        "不低于": "≥",
        "不少于": "≥",
        "至少": "≥",
        "以上": "≥",
        "等于": "=",
    }.get(operator, operator)


def compare_values(left: float, operator: str, right: float) -> Optional[bool]:
    op_map = {
        "小于": lambda a, b: a < b,
        "少于": lambda a, b: a < b,
        "低于": lambda a, b: a < b,
        "大于": lambda a, b: a > b,
        "多于": lambda a, b: a > b,
        "超过": lambda a, b: a > b,
        "不超过": lambda a, b: a <= b,
        "至多": lambda a, b: a <= b,
        "以下": lambda a, b: a <= b,
        "不低于": lambda a, b: a >= b,
        "不少于": lambda a, b: a >= b,
        "至少": lambda a, b: a >= b,
        "以上": lambda a, b: a >= b,
        "等于": lambda a, b: a == b,
        "<": lambda a, b: a < b,
        ">": lambda a, b: a > b,
        "<=": lambda a, b: a <= b,
        ">=": lambda a, b: a >= b,
        "=": lambda a, b: a == b,
        "≤": lambda a, b: a <= b,
        "≥": lambda a, b: a >= b,
    }
    compare = op_map.get(operator)
    return compare(left, right) if compare else None


def is_numeric_comparison(condition: str) -> bool:
    return parse_numeric_comparison(condition) is not None


def is_duration_comparison(condition: str) -> bool:
    cmp_info = parse_numeric_comparison(condition)
    if not cmp_info:
        return False
    if cmp_info.unit not in {"分钟", "小时", "天", "周", "月"}:
        return False
    return any(
        alias in cmp_info.subject
        for alias in (
            "住院天数",
            "住院时间",
            "住院时长",
            "住院日",
            "住了",
            "入院到出院",
            "出院时间",
            "出院日期",
            "离院时间",
        )
    )
