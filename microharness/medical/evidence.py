"""
Structured evidence helpers for medical filter results.

The existing API returns string evidence for compatibility. These helpers add
a Chinese-key structured form that can be rendered by clients without parsing
free text.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from microharness.medical.display_text import sanitize_user_text


@dataclass
class EvidenceItem:
    来源: str
    结论: str
    理由: str
    原文: str
    证据级别: str = "候选证据"

    def to_dict(self) -> dict:
        return asdict(self)


def classify_evidence(matched: bool, reason: str) -> str:
    reason = reason or ""
    if matched:
        return "支持证据"
    if any(token in reason for token in ("不符合", "未找到", "未出现", "无匹配", "无法判断", "不满足")):
        return "反证"
    return "候选证据"


def build_evidence_items(file_results: list[dict]) -> list[dict]:
    items = []
    for file_result in file_results or []:
        matched = bool(file_result.get("matched", False))
        reason = sanitize_user_text(str(file_result.get("reason", "")))
        fields = str(file_result.get("fields", ""))
        role = str(file_result.get("证据角色") or file_result.get("evidence_role") or "")
        if role and role != "主证据":
            conclusion = "辅助依据"
            level = role
        else:
            conclusion = "符合" if matched else "不符合"
            level = classify_evidence(matched, reason)
        confidence = assess_file_confidence(file_result)
        item = EvidenceItem(
            来源=str(file_result.get("file", "")),
            结论=conclusion,
            理由=reason,
            原文=fields,
            证据级别=level,
        ).to_dict() | confidence
        if role:
            item["证据角色"] = role
        if file_result.get("用途"):
            item["用途"] = str(file_result.get("用途"))
        items.append(item)
    return items


UNKNOWN_MARKERS = (
    "无法判断",
    "接口失败",
    "请求超时",
    "外部数据源调用失败",
    "DB不可用",
    "数据库不可用",
    "未取得数据",
    "未取得病历",
    "未取得接口",
    "未找到日期字段",
    "文件中无相关日期/数值字段",
)


def judgment_status(matched: bool, reason: str, per_condition: dict = None) -> tuple[str, bool]:
    texts = [str(reason or "")]
    has_supporting_file = False
    for item in (per_condition or {}).values():
        if isinstance(item, dict):
            texts.append(str(item.get("reason", "")))
            for f in item.get("files", []) or []:
                if isinstance(f, dict):
                    file_reason = str(f.get("reason", "") or "")
                    if f.get("matched") and not any(marker in file_reason for marker in UNKNOWN_MARKERS):
                        has_supporting_file = True
                    texts.append(str(f.get("reason", "")))
    joined = "；".join(texts)
    if matched and has_supporting_file:
        return "符合", True
    if any(marker in joined for marker in UNKNOWN_MARKERS):
        return "无法判断", False
    return ("符合" if matched else "不符合"), True


def _level(score: float) -> str:
    if score >= 0.8:
        return "高"
    if score >= 0.65:
        return "中"
    return "低"


def assess_file_confidence(file_result: dict[str, Any]) -> dict[str, Any]:
    """Estimate confidence from evidence shape, not from concrete query terms."""
    reason = sanitize_user_text(str(file_result.get("reason", "") or ""))
    fields = str(file_result.get("fields", "") or "")
    source = str(file_result.get("file", "") or "")
    matched = bool(file_result.get("matched", False))

    if any(marker in reason for marker in UNKNOWN_MARKERS):
        return {
            "置信度": 0.0,
            "置信等级": "无法判断",
            "依据等级": "数据源不可判定",
            "置信依据": "关键数据源失败或关键证据缺失",
        }

    text = " ".join([reason, fields, source])
    score = 0.62 if not matched else 0.68
    basis = "文本/候选证据"

    if any(token in text for token in ("结果=", "结果：", "异常判断=", "异常状态：", "找到检验项目但结果不符合", "未找到检验项目")):
        score = 0.9
        basis = "结构化检验规则"
    elif any(token in text for token in ("病史年限=", "原文明确否认")):
        score = 0.86
        basis = "结构化病史规则"
    elif any(token in text for token in ("时间窗口内", "与参考时间差", "当天找到", "之后", "之前")):
        score = 0.82
        basis = "结构化时间窗口"
    elif any(token in source for token in ("用药医嘱查询", "诊断查询", "就诊信息查询", "检验指标查询")):
        score = 0.78
        basis = "结构化接口字段"
    elif any(token in reason for token in ("关键字", "结构化字段")):
        score = 0.72
        basis = "结构化字段预筛"

    if "字段映射降级" in text or "未映射字段" in text:
        score = min(score, 0.66)
        basis += "，字段映射降级"
    if file_result.get("cot_response"):
        score = min(score, 0.68)
        basis += "，LLM参与判断"
    if "无相关字段" in reason or "无匹配字段" in fields:
        score = min(score, 0.58)
        basis = "证据字段不足"

    score = round(max(0.0, min(0.99, score)), 2)
    return {
        "置信度": score,
        "置信等级": _level(score),
        "依据等级": basis,
        "置信依据": sanitize_user_text(reason[:120] or basis),
    }


def assess_condition_confidence(condition_result: dict[str, Any]) -> dict[str, Any]:
    files = condition_result.get("files", []) or []
    matched = bool(condition_result.get("matched", False))
    reason = sanitize_user_text(str(condition_result.get("reason", "") or ""))
    status, conclusive = judgment_status(matched, reason, {condition_result.get("condition", ""): condition_result})
    if not conclusive:
        return {
            "判断状态": status,
            "可判定": False,
            "置信度": 0.0,
            "置信等级": "无法判断",
            "依据等级": "数据源不可判定",
        }

    scored_files = [assess_file_confidence(f) for f in files if isinstance(f, dict)]
    numeric_scores = [s["置信度"] for s in scored_files if s.get("置信等级") != "无法判断"]
    if numeric_scores:
        score = max(numeric_scores) if matched else max(numeric_scores)
    else:
        score = 0.6 if reason and reason != "无匹配" else 0.5
    score = round(score, 2)
    return {
        "判断状态": status,
        "可判定": True,
        "置信度": score,
        "置信等级": _level(score),
        "依据等级": "；".join(dict.fromkeys(s.get("依据等级", "") for s in scored_files if s.get("依据等级")))[:120],
    }


def assess_patient_confidence(matched: bool, reason: str, per_condition: dict = None) -> dict[str, Any]:
    reason = sanitize_user_text(str(reason or ""))
    status, conclusive = judgment_status(matched, reason, per_condition)
    if not conclusive:
        return {"判断状态": status, "可判定": False, "置信度": 0.0, "置信等级": "无法判断", "依据等级": "数据源不可判定"}

    condition_scores = []
    for item in (per_condition or {}).values():
        if isinstance(item, dict):
            assessed = item.get("置信评估") or assess_condition_confidence(item)
            if assessed.get("置信等级") != "无法判断":
                condition_scores.append(float(assessed.get("置信度", 0.0)))
    if condition_scores:
        score = min(condition_scores) if matched else max(condition_scores)
    else:
        score = 0.62
    score = round(max(0.0, min(0.99, score)), 2)
    return {"判断状态": status, "可判定": True, "置信度": score, "置信等级": _level(score), "依据等级": "条件证据综合"}
