"""
Canonical intermediate representation for medical filter queries.

The API may receive LLM output, deterministic fallback output, or scheduler
analysis. This module normalizes those variants into a stable structure so
later stages do not need to understand every upstream format.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import re
from typing import Optional

from .temporal_parser import parse_numeric_comparison


@dataclass
class ConditionIR:
    text: str
    keyword: str = ""
    entity: str = ""
    entity_type: str = ""
    predicate: str = ""
    modifiers: list[str] = field(default_factory=list)
    target_docs: list[str] = field(default_factory=list)
    target_sections: list[str] = field(default_factory=list)
    target_services: list[str] = field(default_factory=list)
    is_numeric: bool = False
    semantic_class: str = ""
    numeric_comparison: Optional[dict] = None

    def to_dict(self) -> dict:
        return {
            "条件文本": self.text,
            "核心词": self.keyword,
            "实体": self.entity,
            "实体类型": self.entity_type,
            "谓词": self.predicate,
            "修饰词": self.modifiers,
            "目标文档": self.target_docs,
            "目标章节": self.target_sections,
            "目标服务": self.target_services,
            "是否数值条件": self.is_numeric,
            "语义类型": self.semantic_class,
            "数值比较": self.numeric_comparison,
        }


@dataclass
class QueryIR:
    original: str
    query_type: str = "simple"
    connector: Optional[str] = None
    negated: bool = False
    source: str = ""
    conditions: list[ConditionIR] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "原始条件": self.original,
            "类型": self.query_type,
            "连接关系": self.connector,
            "是否取反": self.negated,
            "来源": self.source,
            "子条件": [condition.to_dict() for condition in self.conditions],
        }


def build_query_ir(analysis: dict, original_condition: str) -> QueryIR:
    conditions = []
    for cond in analysis.get("conditions", []) or []:
        text = cond.get("text") or original_condition
        has_explicit_numeric_predicate = bool(re.search(
            r"(>=|<=|>|<|=|\u2265|\u2264|\uff1e|\uff1c|\u5927\u4e8e|\u5c0f\u4e8e|\u9ad8\u4e8e|\u4f4e\u4e8e|\u8d85\u8fc7|\u4e0d\u5c11\u4e8e|\u4e0d\u4f4e\u4e8e|\u4e0d\u8d85\u8fc7|\u81f3\u591a|\u81f3\u5c11|\u7b49\u4e8e)",
            text or "",
        ))
        cmp_info = (
            parse_numeric_comparison(text)
            if has_explicit_numeric_predicate
            else None
        )
        has_explicit_predicate = bool(re.search(
            r"(>|<|>=|<=|=|≥|≤|＞|＜|大于|小于|高于|低于|超过|不少于|不低于|不超过|至多|至少|等于|偏高|偏低|异常|正常)",
            text or "",
        ))
        conditions.append(
            ConditionIR(
                text=text,
                keyword=cond.get("keyword", "") or text,
                entity=cond.get("entity", "") or cond.get("keyword", "") or text,
                entity_type=cond.get("entity_type", ""),
                predicate=cond.get("predicate", ""),
                modifiers=list(cond.get("modifiers", []) or []),
                target_docs=list(cond.get("target_docs", []) or []),
                target_sections=list(cond.get("target_sections", []) or []),
                target_services=list(cond.get("target_skills", []) or []),
                is_numeric=bool(cond.get("is_numeric", False)) and has_explicit_predicate,
                semantic_class=cond.get("semantic_class", ""),
                numeric_comparison=cmp_info.to_dict() if (cmp_info and has_explicit_predicate) else None,
            )
        )
    if not conditions:
        conditions.append(ConditionIR(text=original_condition, keyword=original_condition))

    return QueryIR(
        original=original_condition,
        query_type=analysis.get("type", "simple"),
        connector=analysis.get("connector"),
        negated=bool(analysis.get("negated", False)),
        source=analysis.get("source", ""),
        conditions=conditions,
    )
