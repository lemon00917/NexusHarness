"""Generic medical entity normalization helpers.

The language model may provide a canonical entity and aliases. This module
only validates, deduplicates, and exposes those candidates to deterministic
evidence matchers; it does not contain disease, drug, or lab dictionaries.
"""

from __future__ import annotations

import re
from typing import Any


_FUNCTION_WORDS_RE = re.compile(
    r"^(?:\u6709|\u5b58\u5728|\u60a3\u6709|\u8bca\u65ad\u4e3a|\u8bca\u65ad\u6709|\u6709\u8bca\u65ad\u6709|"
    r"\u5f00\u8fc7|\u5f00\u7acb\u8fc7|\u4f7f\u7528\u8fc7|\u670d\u7528\u8fc7|\u6ce8\u5c04\u8fc7|\u7684\u60a3\u8005|\u7684\u75c5\u4eba|\u7684\u75c5\u4f8b)+"
)
_QUERY_SUBJECT_PREFIX_RE = re.compile(
    r"^(?:(?:\u8be5|\u6b64)?(?:\u60a3\u8005|\u75c5\u4eba|\u75c5\u4f8b|\u60a3\u513f))(?:\u662f\u5426|\u6709\u65e0|\u7684)?"
)
_QUERY_SUBJECT_SUFFIX_RE = re.compile(
    r"(?:\u7684)?(?:\u60a3\u8005|\u75c5\u4eba|\u75c5\u4f8b|\u60a3\u513f)$"
)


def _clean(value: Any) -> str:
    text = str(value or "").strip()
    text = re.sub(r"[\s\u3000]+", "", text)
    return text


def _key(value: str) -> str:
    return re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", value).lower()


def _normalize_candidate(value: Any) -> str:
    text = _clean(value)
    if not text:
        return ""
    previous = None
    while text and text != previous:
        previous = text
        text = _QUERY_SUBJECT_PREFIX_RE.sub("", text)
        text = _FUNCTION_WORDS_RE.sub("", text)
        text = _QUERY_SUBJECT_SUFFIX_RE.sub("", text)
    return text


def _candidate_values(cond: dict) -> list[str]:
    values: list[str] = []
    canonical = cond.get("canonical_entity") or cond.get("entity") or cond.get("keyword")
    for value in [canonical, cond.get("entity"), cond.get("keyword")]:
        cleaned = _clean(value)
        if cleaned:
            values.append(cleaned)
    aliases = cond.get("aliases")
    if aliases is None:
        aliases = cond.get("entity_aliases")
    if isinstance(aliases, str):
        aliases = [aliases]
    if isinstance(aliases, (list, tuple, set)):
        values.extend(_clean(value) for value in aliases)

    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        value = _normalize_candidate(value)
        key = _key(value)
        if not value or not key or key in seen:
            continue
        seen.add(key)
        output.append(value)
    return output


def normalize_entity_fields(cond: dict) -> dict:
    """Normalize entity fields in-place while preserving the original wording."""
    if not isinstance(cond, dict):
        return cond
    candidates = _candidate_values(cond)
    if not candidates:
        return cond

    canonical = _normalize_candidate(cond.get("canonical_entity")) or candidates[0]
    cond["canonical_entity"] = canonical
    cond["entity"] = _normalize_candidate(cond.get("entity")) or canonical
    cond["keyword"] = _normalize_candidate(cond.get("keyword")) or canonical
    cond["aliases"] = [value for value in candidates if _key(value) != _key(canonical)]

    confidence = cond.get("entity_confidence")
    try:
        confidence = max(0.0, min(1.0, float(confidence))) if confidence not in (None, "") else None
    except (TypeError, ValueError):
        confidence = None
    cond["entity_confidence"] = confidence
    source = _clean(cond.get("normalization_source"))
    if source:
        cond["normalization_source"] = source
    elif cond.get("aliases"):
        cond["normalization_source"] = "llm"
    else:
        cond["normalization_source"] = "deterministic"
    cond["entity_candidates"] = candidates
    return cond


def entity_candidates(value: str = "", semantic: dict | None = None) -> list[str]:
    """Return canonical and alias candidates, with legacy single-value fallback."""
    semantic = semantic if isinstance(semantic, dict) else {}
    values: list[Any] = [
        value,
        semantic.get("canonical_entity"),
        semantic.get("entity"),
        semantic.get("keyword"),
    ]
    for field_name in ("entity_candidates", "aliases", "entity_aliases"):
        candidates = semantic.get(field_name)
        if isinstance(candidates, str):
            values.append(candidates)
        elif isinstance(candidates, (list, tuple, set)):
            values.extend(candidates)
    output: list[str] = []
    seen: set[str] = set()
    for item in values:
        text = _normalize_candidate(item)
        key = _key(text)
        if text and key not in seen:
            seen.add(key)
            output.append(text)
    return output
