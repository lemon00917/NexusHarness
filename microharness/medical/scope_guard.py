"""Capability boundary checks for medical record filter queries."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import re
from typing import Any


@dataclass(frozen=True)
class ScopeDecision:
    allowed: bool
    code: str = "allowed"
    category: str = "medical_filter"
    reason: str = ""
    signals: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_VAGUE_PATTERNS = (
    r"^(?:\u8fd9\u4e2a|\u8be5|\u672c\u6b21)?\u60a3\u8005(?:\u600e\u4e48\u6837|\u5982\u4f55|\u4ec0\u4e48\u60c5\u51b5|\u6709\u95ee\u9898\u5417)$",
    r"^(?:\u5e2e\u6211)?(?:\u67e5\u4e00\u4e0b|\u770b\u4e00\u4e0b|\u5206\u6790\u4e00\u4e0b)(?:\u8fd9\u4e2a|\u8be5)?\u60a3\u8005?$",
    r"^(?:\u6709\u6ca1\u6709|\u6709\u65e0|\u662f\u5426\u6709)?\u95ee\u9898$",
    r"^(?:\u770b\u770b|\u67e5\u67e5)(?:\u75c5\u5386|\u60a3\u8005\u60c5\u51b5)?$",
)

_UNSUPPORTED_PATTERNS = (
    (r"(?:CT|MRI|MR|X\u7ebf|\u8d85\u58f0|\u5f71\u50cf).{0,8}(?:\u539f\u56fe|\u539f\u59cb\u56fe\u50cf|\u56fe\u50cf\u8bca\u65ad)", "imaging_pixels"),
    (r"(?:\u75c5\u7406\u5207\u7247|\u5207\u7247\u56fe\u50cf|\u6570\u5b57\u75c5\u7406\u56fe\u50cf)", "pathology_images"),
    (r"(?:\u57fa\u56e0\u6d4b\u5e8f|\u5168\u57fa\u56e0\u7ec4|\u5168\u5916\u663e\u5b50|\u57fa\u56e0\u7a81\u53d8|\u6d4b\u5e8f\u7ed3\u679c)", "genomics"),
    (r"(?:\u9662\u5916\u957f\u671f\u968f\u8bbf|\u53ef\u7a7f\u6234\u8bbe\u5907|\u667a\u80fd\u624b\u73af|\u5bb6\u5ead\u76d1\u6d4b\u8bbe\u5907)", "external_followup"),
)

_UNRELATED_PATTERNS = (
    (r"(?:\u5929\u6c14|\u6c14\u6e29|\u7a7a\u6c14\u8d28\u91cf|\u4f1a\u4e0d\u4f1a\u4e0b\u96e8)", "weather"),
    (r"(?:\u80a1\u7968|\u57fa\u91d1\u884c\u60c5|\u6c47\u7387|\u8bc1\u5238|\u671f\u8d27)", "finance"),
    (r"(?:\u673a\u7968|\u9152\u5e97|\u65c5\u6e38\u653b\u7565|\u884c\u7a0b\u89c4\u5212)", "travel"),
    (r"(?:\u5199|\u4fee\u6539|\u8c03\u8bd5|\u751f\u6210).{0,8}(?:\u4ee3\u7801|\u7a0b\u5e8f|\u811a\u672c|\u7f51\u9875)", "software"),
    (r"(?:\u7ffb\u8bd1\u6210|\u4e2d\u82f1\u7ffb\u8bd1|\u82f1\u8bd1\u4e2d|\u6c49\u8bd1\u82f1)", "translation"),
)

_NON_FILTER_MEDICAL_PATTERNS = (
    (r"^(?:\u4ec0\u4e48\u662f|\u4ecb\u7ecd\u4e00\u4e0b|\u89e3\u91ca\u4e00\u4e0b).+", "medical_knowledge"),
    (r"(?:\u600e\u4e48|\u5982\u4f55|\u5e94\u8be5\u600e\u6837).{0,10}(?:\u6cbb\u7597|\u7528\u836f|\u5904\u7406|\u62a4\u7406|\u5eb7\u590d)", "treatment_advice"),
    (r"(?:\u63a8\u8350|\u5236\u5b9a).{0,8}(?:\u836f\u7269|\u7528\u836f|\u6cbb\u7597\u65b9\u6848|\u8bca\u7597\u65b9\u6848)", "treatment_advice"),
    (
        r"(?:\u4ecb\u7ecd|\u89e3\u91ca|\u67e5\u8be2|\u67e5\u770b).{0,8}"
        r"(?:\u836f\u54c1\u8bf4\u660e\u4e66|\u7528\u6cd5\u7528\u91cf|\u836f\u7269\u526f\u4f5c\u7528|\u7528\u836f\u7981\u5fcc)",
        "drug_information",
    ),
)


def _compact(text: str) -> str:
    return re.sub(r"[\s\u3000\uff0c,\u3002\uff1b;\u3001\uff01!\uff1f?\uff1a:]+", "", str(text or "")).strip()


def _first_signal(text: str, patterns: tuple[tuple[str, str], ...]) -> str:
    for pattern, signal in patterns:
        if re.search(pattern, text, re.I):
            return signal
    return ""


def evaluate_medical_filter_scope(
    condition: str,
    analysis: dict[str, Any] | None = None,
) -> ScopeDecision:
    """Return whether a request belongs to the patient-record filter capability.

    The guard intentionally rejects only strong signals. Unknown symptoms,
    diagnoses, or routes stay executable so catalog growth does not require a
    matching guard change.
    """
    del analysis  # Reserved for future structured capability checks.
    text = _compact(condition)
    if not text:
        return ScopeDecision(
            allowed=False,
            code="empty_condition",
            category="ambiguous_request",
            reason="\u8bf7\u63d0\u4f9b\u5177\u4f53\u7684\u75c5\u5386\u7b5b\u9009\u6761\u4ef6\u3002",
            signals=["empty_condition"],
        )

    if any(re.search(pattern, text, re.I) for pattern in _VAGUE_PATTERNS):
        return ScopeDecision(
            allowed=False,
            code="ambiguous_request",
            category="ambiguous_request",
            reason=(
                "\u5f53\u524d\u8bf7\u6c42\u6ca1\u6709\u53ef\u6267\u884c\u7684\u75c5\u5386\u7b5b\u9009\u6761\u4ef6\uff0c"
                "\u8bf7\u8865\u5145\u75c7\u72b6\u3001\u8bca\u65ad\u3001\u7528\u836f\u3001\u68c0\u9a8c\u6307\u6807\u6216\u65f6\u95f4\u8303\u56f4\u3002"
            ),
            signals=["vague_filter_condition"],
        )

    signal = _first_signal(text, _UNSUPPORTED_PATTERNS)
    if signal:
        return ScopeDecision(
            allowed=False,
            code="unsupported_data_source",
            category="unsupported_data_source",
            reason=(
                "\u5f53\u524d\u75c5\u5386\u7b5b\u9009\u80fd\u529b\u6ca1\u6709\u63a5\u5165\u8be5\u7c7b\u6570\u636e\u6e90\uff0c"
                "\u65e0\u6cd5\u57fa\u4e8e\u8be5\u6570\u636e\u6267\u884c\u7b5b\u9009\u3002"
            ),
            signals=[signal],
        )

    signal = _first_signal(text, _UNRELATED_PATTERNS)
    if signal:
        return ScopeDecision(
            allowed=False,
            code="unrelated_request",
            category="unrelated_request",
            reason="\u5f53\u524d\u8bf7\u6c42\u4e0d\u5c5e\u4e8e\u75c5\u5386\u6761\u4ef6\u7b5b\u9009\u8303\u56f4\u3002",
            signals=[signal],
        )

    signal = _first_signal(text, _NON_FILTER_MEDICAL_PATTERNS)
    if signal:
        return ScopeDecision(
            allowed=False,
            code="non_filter_medical_request",
            category="non_filter_medical_request",
            reason=(
                "\u5f53\u524d\u8bf7\u6c42\u5c5e\u4e8e\u533b\u5b66\u77e5\u8bc6\u6216\u8bca\u7597\u5efa\u8bae\uff0c"
                "\u4e0d\u662f\u53ef\u6267\u884c\u7684\u75c5\u5386\u6761\u4ef6\u7b5b\u9009\u3002"
            ),
            signals=[signal],
        )

    return ScopeDecision(allowed=True)


def build_scope_rejection_response(
    condition: str,
    decision: ScopeDecision,
    *,
    original_condition: str | None = None,
    query_ir: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a frontend-compatible response for a rejected scope decision."""
    diagnostic = decision.to_dict()
    reason = decision.reason or "\u5f53\u524d\u8bf7\u6c42\u65e0\u6cd5\u4f5c\u4e3a\u75c5\u5386\u7b5b\u9009\u6761\u4ef6\u6267\u884c\u3002"
    item = {
        "matched": False,
        "\u5224\u65ad\u72b6\u6001": "\u65e0\u6cd5\u6267\u884c",
        "\u53ef\u5224\u5b9a": False,
        "\u7f6e\u4fe1\u5ea6": 0.0,
        "\u7f6e\u4fe1\u7b49\u7ea7": "\u65e0\u6cd5\u5224\u65ad",
        "\u4f9d\u636e\u7b49\u7ea7": "\u8bf7\u6c42\u8fb9\u754c\u62d2\u7edd",
        "reason": reason,
        "\u7528\u6237\u89e3\u91ca": reason,
        "per_condition": {},
        "scope_guard": diagnostic,
    }
    response = {
        "matched_count": 0,
        "\u5224\u65ad\u72b6\u6001": "\u65e0\u6cd5\u6267\u884c",
        "\u53ef\u5224\u5b9a": False,
        "\u7f6e\u4fe1\u5ea6": 0.0,
        "\u7f6e\u4fe1\u7b49\u7ea7": "\u65e0\u6cd5\u5224\u65ad",
        "\u4f9d\u636e\u7b49\u7ea7": "\u8bf7\u6c42\u8fb9\u754c\u62d2\u7edd",
        "reason": reason,
        "\u7528\u6237\u89e3\u91ca": reason,
        "results": [item],
        "scope_guard": diagnostic,
        "\u539f\u59cb\u6761\u4ef6": original_condition or condition,
        "\u89c4\u8303\u6761\u4ef6": condition,
    }
    if query_ir is not None:
        response["\u67e5\u8be2IR"] = query_ir
    return response
