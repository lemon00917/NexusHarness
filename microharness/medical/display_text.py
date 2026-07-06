"""User-facing text cleanup for medical filter responses.

Internal rules may use compact enum values such as high/low/normal for
deterministic computation. API responses should present Chinese clinical words
instead of leaking these internal tokens.
"""

from __future__ import annotations

import re
from typing import Any


_STATUS_LABELS = {
    "high": "高于参考范围",
    "low": "低于参考范围",
    "normal": "参考范围内",
    "abnormal": "异常",
    "unknown": "未知",
    "true": "是",
    "false": "否",
    "matched": "匹配",
    "unmatched": "未匹配",
    "fallback": "兜底分析",
    "deterministic": "规则归一化",
    "concept_match": "概念匹配",
    "metadata": "元数据匹配",
}


def display_status(value: str) -> str:
    return _STATUS_LABELS.get(str(value or "").strip().lower(), str(value or "未知"))


def sanitize_user_text(text: str) -> str:
    cleaned = str(text or "")

    def repl_status(match: re.Match) -> str:
        label = match.group(1)
        value = match.group(2)
        return f"{label}：{display_status(value)}"

    cleaned = re.sub(
        r"(异常判断|异常状态)\s*=\s*(high|low|normal|abnormal|unknown)\b",
        repl_status,
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"(source|来源)\s*[:=]\s*(fallback|deterministic|concept_match|metadata)\b",
        lambda m: f"来源：{display_status(m.group(2))}",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"\b(high|low|normal|abnormal|unknown|matched|unmatched)\b",
        lambda m: display_status(m.group(1)),
        cleaned,
        flags=re.IGNORECASE,
    )
    return cleaned


def naturalize_user_text(text: str) -> str:
    """Convert common structured evidence strings into readable Chinese."""
    cleaned = sanitize_user_text(text)
    lab_outside = re.search(
        r"找到检验项目'(?P<item>[^']+)'，但检测时间不在(?P<scope>[^（]+)"
        r"（范围：(?P<start>[^至]+) 至 (?P<end>[^）]+)）：.*?"
        r"检测时间[=:：](?P<time>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})，"
        r"结果[=:：](?P<value>[^，]+)，异常标志[=:：](?P<flag>[^，]+)，异常状态[=:：](?P<status>[^，；]+)",
        cleaned,
    )
    if lab_outside:
        g = lab_outside.groupdict()
        return (
            f"检验接口找到{g['item']}记录，但检测时间为{g['time']}，"
            f"不在{g['scope']}范围内（{g['start']}至{g['end']}）；"
            f"该次结果为{g['value']}，异常标志为{g['flag']}，{g['status']}。"
        )
    return cleaned


def sanitize_response_text(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: sanitize_response_text(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize_response_text(v) for v in obj]
    if isinstance(obj, str):
        return sanitize_user_text(obj)
    return obj
