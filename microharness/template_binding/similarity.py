"""Deterministic text similarity helpers for template binding."""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from functools import lru_cache
from typing import Iterable


_NON_WORD_RE = re.compile(r"[^0-9a-z\u4e00-\u9fff]+")
_VERSION_RE = re.compile(r"(?:\u7248\u672c|v)\d+(?:\.\d+)*$", re.IGNORECASE)

# These suffix families describe how a template represents a value, rather
# than what the value means.  They are intentionally applied to every label
# instead of enumerating clinical field names in the node matcher.
_LABEL_SUFFIX_FAMILIES = (
    ("datetime", "timestamp", "date", "time"),
    ("日期时间", "时间戳", "日期", "时间", "时刻"),
    ("content", "text", "value", "field"),
    ("内容", "文本", "字段", "数值", "值"),
)

# Keep only terms that are interchangeable without document context. Fields
# such as encounter number, inpatient number, and medical-record number are
# intentionally excluded because hospitals may assign them independently.
_LABEL_EQUIVALENT_GROUPS = (
    ("婚姻状况", "婚姻状态"),
    ("nation", "民族"),
    ("诊疗经过", "诊治经过", "治疗经过"),
)


@lru_cache(maxsize=32768)
def _normalize_text_cached(value: str) -> str:
    text = unicodedata.normalize("NFKC", value).strip().lower()
    text = _NON_WORD_RE.sub("", text)
    return _VERSION_RE.sub("", text)


def normalize_text(value: object) -> str:
    return _normalize_text_cached(str(value or ""))


@lru_cache(maxsize=65536)
def _label_variants_cached(normalized: str) -> tuple[str, ...]:
    """Return generic, meaning-preserving forms of a field label.

    A label such as ``admissionDateTime`` and ``admissionDate`` differs only
    in value precision.  Likewise, ``diagnosisText`` and ``diagnosis`` use a
    different leaf representation.  The stem is retained only when it still
    has at least two characters, preventing a bare ``text``/``value`` node
    from becoming a meaningful business field.
    """
    if not normalized:
        return ()
    forms = {normalized}
    for family in _LABEL_SUFFIX_FAMILIES:
        matched = next(
            (
                suffix
                for suffix in sorted(family, key=len, reverse=True)
                if normalized.endswith(suffix)
                and len(normalized) - len(suffix) >= 2
            ),
            None,
        )
        if matched is None:
            continue
        stem = normalized[: -len(matched)]
        forms.add(stem)
        forms.update(stem + suffix for suffix in family)
    for group in _LABEL_EQUIVALENT_GROUPS:
        normalized_group = {normalize_text(item) for item in group}
        if normalized in normalized_group:
            forms.update(normalized_group)
    return tuple(sorted(forms))


def label_variants(value: object) -> tuple[str, ...]:
    """Normalize a label and expand generic representation variants."""
    return _label_variants_cached(normalize_text(value))


@lru_cache(maxsize=131072)
def _text_similarity_normalized(left: str, right: str) -> float:
    """Score normalized text and reuse repeated feature comparisons."""
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    if left > right:
        left, right = right, left
    shorter, longer = sorted((left, right), key=len)
    containment = len(shorter) / len(longer) if shorter in longer and len(shorter) >= 2 else 0.0
    sequence = SequenceMatcher(None, left, right).ratio()
    left_chars = set(left)
    right_chars = set(right)
    union = left_chars | right_chars
    jaccard = len(left_chars & right_chars) / len(union) if union else 0.0
    return round(max(containment * 0.96, sequence * 0.62 + jaccard * 0.38), 6)


def text_similarity(left: object, right: object) -> float:
    return _text_similarity_normalized(normalize_text(left), normalize_text(right))


def best_similarity_normalized(
    left_values: Iterable[str], right_values: Iterable[str]
) -> float:
    """Return the best score for values that have already been normalized."""
    left = tuple(value for value in left_values if value)
    right = tuple(value for value in right_values if value)
    if not left or not right:
        return 0.0
    return max(_text_similarity_normalized(a, b) for a in left for b in right)


def best_similarity(left_values: Iterable[object], right_values: Iterable[object]) -> float:
    return best_similarity_normalized(
        (normalize_text(value) for value in left_values),
        (normalize_text(value) for value in right_values),
    )


def split_path(value: object) -> list[str]:
    return [part.strip() for part in re.split(r"[/\\>|]+", str(value or "")) if part.strip()]
