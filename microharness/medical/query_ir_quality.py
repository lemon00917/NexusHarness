"""Quality gate for executable medical query IR."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import re
from typing import Any

from microharness.medical.query_ir import QueryIR
from microharness.medical.query_ir_validator import is_executable_numeric_condition
from microharness.medical.semantic_rules import split_compound_clauses


@dataclass(frozen=True)
class IRQualityIssue:
    code: str
    message: str
    condition_id: str = ""
    blocking: bool = True
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class IRQualityAssessment:
    valid: bool
    score: float
    issues: list[IRQualityIssue] = field(default_factory=list)
    warnings: list[IRQualityIssue] = field(default_factory=list)

    @property
    def retry_recommended(self) -> bool:
        return bool(self.issues)

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "score": self.score,
            "retry_recommended": self.retry_recommended,
            "issues": [item.to_dict() for item in self.issues],
            "warnings": [item.to_dict() for item in self.warnings],
        }


_DURATION = r"(?:\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百千万亿]+)\s*(?:分钟|小时|天|日|周|月|年)"
_EVENT_RELATION = re.compile(r"(?:手术|术|入院|出院)\s*[前后]|(?:手术|术|入院|出院)[前后]")
_BOUNDED_WINDOW = re.compile(rf"{_DURATION}\s*内")
_ANCHORED_WINDOW = re.compile(
    rf"(?:最近|手术前|手术后|术前|术后|入院前|入院后|出院前|出院后|入院|出院)\s*{_DURATION}\s*内"
)
_SOURCE_UNIT = re.compile(r"(?:岁|天|日|小时|分钟|周|月|年|%|mmol/L|mg/L|g/L|10\s*[×xX*^]?\s*[⁰¹²³⁴⁵⁶⁷⁸⁹0-9]+\s*/\s*[Ll])", re.IGNORECASE)


def assess_query_ir(query_ir: QueryIR, original_condition: str, analysis: dict | None = None) -> IRQualityAssessment:
    """Check whether an IR is complete enough to enter evidence execution."""
    blocking: list[IRQualityIssue] = []
    warnings: list[IRQualityIssue] = []
    conditions = list(query_ir.conditions or [])

    if not conditions:
        blocking.append(IRQualityIssue("MISSING_CONDITIONS", "未识别到可执行的筛选条件"))

    expected_parts, _ = split_compound_clauses(original_condition)
    if len(expected_parts) > 1 and len(conditions) != len(expected_parts):
        blocking.append(
            IRQualityIssue(
                "COMPOUND_STRUCTURE_UNRESOLVED",
                f"原句包含{len(expected_parts)}个明确子条件，但IR仅保留{len(conditions)}个",
                details={"expected_count": len(expected_parts), "actual_count": len(conditions)},
            )
        )

    for condition in conditions:
        condition_id = condition.condition_id
        text = str(condition.text or "").strip()
        if not text:
            blocking.append(IRQualityIssue("MISSING_CONDITION_TEXT", "子条件文本为空", condition_id))
            continue

        if _EVENT_RELATION.search(text):
            temporal = condition.temporal
            if not temporal or not temporal.event or temporal.relation not in {"before", "after"}:
                blocking.append(
                    IRQualityIssue(
                        "TEMPORAL_EVENT_UNRESOLVED",
                        "时间条件包含事件前后关系，但未形成可靠的事件锚点",
                        condition_id,
                    )
                )

        if _BOUNDED_WINDOW.search(text) and not _ANCHORED_WINDOW.search(text):
            temporal = condition.temporal
            has_structured_anchor = bool(
                temporal
                and (
                    (temporal.event and temporal.relation in {"before", "after", "during"})
                    or temporal.relation == "recent"
                    or temporal.scope == "encounter"
                )
            )
            if not has_structured_anchor:
                blocking.append(
                    IRQualityIssue(
                        "TEMPORAL_ANCHOR_MISSING",
                        "时间窗缺少参照事件，无法确定从哪个时间点计算",
                        condition_id,
                    )
                )

        if is_executable_numeric_condition(text):
            comparison = condition.numeric_comparison or {}
            if not comparison or comparison.get("threshold") is None or not comparison.get("operator"):
                blocking.append(
                    IRQualityIssue(
                        "NUMERIC_COMPARISON_MISSING",
                        "显式数值条件未完整保留比较符或阈值",
                        condition_id,
                    )
                )
            elif not str(comparison.get("subject") or "").strip():
                blocking.append(
                    IRQualityIssue(
                        "NUMERIC_SUBJECT_MISSING",
                        "数值比较缺少指标或属性主体",
                        condition_id,
                    )
                )
            if _SOURCE_UNIT.search(text) and not str(comparison.get("unit") or "").strip():
                warnings.append(
                    IRQualityIssue(
                        "NUMERIC_UNIT_NOT_NORMALIZED",
                        "原句包含单位，但IR尚未完成单位归一化",
                        condition_id,
                        blocking=False,
                    )
                )

        quantifier = condition.quantifier
        if quantifier and quantifier.mode in {"at_least", "more_than", "exact"} and quantifier.count is None:
            blocking.append(
                IRQualityIssue(
                    "QUANTIFIER_COUNT_MISSING",
                    "次数约束缺少具体数量",
                    condition_id,
                )
            )

        if condition.domain in {"", "unknown"}:
            warnings.append(
                IRQualityIssue(
                    "DOMAIN_UNCERTAIN",
                    "条件领域尚未可靠归一，后续只能按通用临床概念规划证据",
                    condition_id,
                    blocking=False,
                )
            )

    for item in ((analysis or {}).get("ir_validation", {}).get("issues", []) or []):
        if isinstance(item, dict):
            warnings.append(
                IRQualityIssue(
                    str(item.get("code") or "IR_REPAIRED"),
                    str(item.get("message") or "查询结构经过确定性修复"),
                    f"c{int(item.get('condition_index', 0)) + 1}",
                    blocking=False,
                )
            )

    score = round(max(0.0, 1.0 - 0.25 * len(blocking) - 0.05 * len(warnings)), 2)
    return IRQualityAssessment(valid=not blocking, score=score, issues=blocking, warnings=warnings)


def build_ir_ambiguity_response(
    condition: str,
    query_ir: QueryIR,
    assessment: IRQualityAssessment,
    *,
    original_condition: str | None = None,
    analysis: dict | None = None,
    retried: bool = False,
) -> dict[str, Any]:
    """Return a frontend-compatible UNKNOWN response without executing evidence queries."""
    issue_text = "；".join(dict.fromkeys(item.message for item in assessment.issues))
    reason = f"筛选条件存在关键歧义，无法可靠执行：{issue_text or '查询结构不完整'}"
    diagnostic = assessment.to_dict() | {"retried": retried, "error_code": "AMBIGUOUS_QUERY_IR"}
    item = {
        "matched": False,
        "判断状态": "无法判断",
        "可判定": False,
        "置信度": 0.0,
        "置信等级": "无法判断",
        "依据等级": "查询语义不完整",
        "reason": reason,
        "用户解释": reason,
        "per_condition": {},
        "error_code": "AMBIGUOUS_QUERY_IR",
        "ir_quality": diagnostic,
    }
    return {
        "condition": condition,
        "原始条件": original_condition or condition,
        "规范条件": condition,
        "route": analysis or {},
        "查询IR": query_ir.to_dict(),
        "matched_count": 0,
        "判断状态": "无法判断",
        "可判定": False,
        "置信度": 0.0,
        "置信等级": "无法判断",
        "依据等级": "查询语义不完整",
        "reason": reason,
        "用户解释": reason,
        "results": [item],
        "error_code": "AMBIGUOUS_QUERY_IR",
        "ir_quality": diagnostic,
    }
