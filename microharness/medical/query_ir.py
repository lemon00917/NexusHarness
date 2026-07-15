"""
Canonical intermediate representation for medical filter queries.

The API may receive LLM output, deterministic fallback output, or scheduler
analysis. This module normalizes those variants into a stable structure so
later stages do not need to understand every upstream format.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import re
from typing import Any, Optional

from .temporal_parser import parse_cn_number, parse_numeric_comparison


@dataclass
class TemporalIR:
    scope: str = ""
    event: str = ""
    relation: str = ""
    duration: Optional[float] = None
    unit: str = ""
    selection: str = ""
    raw: str = ""

    def to_dict(self) -> dict:
        return {
            "范围": self.scope,
            "事件": self.event,
            "关系": self.relation,
            "时长": self.duration,
            "单位": self.unit,
            "事件选择": self.selection,
            "原始表达": self.raw,
        }


@dataclass
class AssertionIR:
    present: Optional[bool] = None
    certainty: str = ""
    subject: str = "patient"
    temporal_context: str = ""

    def to_dict(self) -> dict:
        return {
            "是否存在": self.present,
            "确定性": self.certainty,
            "主体": self.subject,
            "时间语境": self.temporal_context,
        }


@dataclass
class QuantifierIR:
    mode: str = ""
    count: Optional[float] = None
    unit: str = ""

    def to_dict(self) -> dict:
        return {"模式": self.mode, "次数": self.count, "单位": self.unit}


@dataclass
class ConditionIR:
    text: str
    condition_id: str = ""
    domain: str = ""
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
    temporal: Optional[TemporalIR] = None
    assertion: Optional[AssertionIR] = None
    quantifier: Optional[QuantifierIR] = None
    depends_on: list[str] = field(default_factory=list)
    attributes: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "条件ID": self.condition_id,
            "条件文本": self.text,
            "领域": self.domain,
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
            "时间约束": self.temporal.to_dict() if self.temporal else None,
            "断言": self.assertion.to_dict() if self.assertion else None,
            "量词": self.quantifier.to_dict() if self.quantifier else None,
            "依赖": self.depends_on,
            "属性": self.attributes,
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


_ENTITY_DOMAIN_MAP = {
    "drug": "medication",
    "medication": "medication",
    "lab": "laboratory",
    "laboratory": "laboratory",
    "diagnosis": "diagnosis",
    "disease": "diagnosis",
    "symptom": "symptom",
    "sign": "clinical_sign",
    "procedure": "procedure",
    "surgery": "procedure",
    "demographic": "demographic",
    "encounter": "encounter",
}

_SERVICE_DOMAIN_MAP = {
    "drug-interaction": "medication",
    "lab-results": "laboratory",
    "encounter-info": "encounter",
    "patient-info": "demographic",
    "diagnosis": "diagnosis",
    "diagnosis-query": "diagnosis",
}

_DURATION_PATTERN = r"([0-9]+(?:\.[0-9]+)?|[零一二两三四五六七八九十百]+)\s*(分钟|小时|天|日|周|月|年)"


def _coerce_mapping(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _infer_domain(cond: dict, text: str) -> str:
    explicit = str(cond.get("domain") or "").strip().lower()
    if explicit:
        return explicit

    entity_type = str(cond.get("entity_type") or "").strip().lower()
    if entity_type in _ENTITY_DOMAIN_MAP:
        return _ENTITY_DOMAIN_MAP[entity_type]

    keyword = str(cond.get("keyword") or "")
    if re.search(r"年龄|岁以上|岁以下|性别|出生日期", text + keyword):
        return "demographic"
    if re.search(r"住院天数|住院时长|入院时间|出院时间", text + keyword):
        return "encounter"

    services = list(cond.get("target_skills", []) or [])
    service_domains = {
        _SERVICE_DOMAIN_MAP[service]
        for service in services
        if service in _SERVICE_DOMAIN_MAP
    }
    if len(service_domains) == 1:
        return next(iter(service_domains))

    semantic_class = str(cond.get("semantic_class") or "")
    if "检验" in semantic_class:
        return "laboratory"
    if "用药" in semantic_class or "医嘱" in semantic_class:
        return "medication"
    if "住院" in semantic_class or "就诊" in semantic_class:
        return "encounter"
    return "clinical_concept"


def _duration_value(raw: str) -> Optional[float]:
    value = parse_cn_number(str(raw or ""))
    return float(value) if value is not None else None


def _optional_number(value: object) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return _duration_value(str(value))


def _optional_bool(value: object) -> Optional[bool]:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"true", "1", "yes", "present", "是", "存在"}:
        return True
    if normalized in {"false", "0", "no", "absent", "否", "不存在"}:
        return False
    return None


def _parse_temporal_ir(text: str, supplied: object = None) -> Optional[TemporalIR]:
    data = _coerce_mapping(supplied)
    if data:
        return TemporalIR(
            scope=str(data.get("scope") or ""),
            event=str(data.get("event") or data.get("anchor_event") or ""),
            relation=str(data.get("relation") or data.get("direction") or ""),
            duration=_optional_number(data.get("duration")),
            unit=str(data.get("unit") or ""),
            selection=str(data.get("selection") or data.get("anchor_selection") or ""),
            raw=str(data.get("raw") or text),
        )

    event_patterns = (
        (r"(?:手术|术)(前|后)\s*" + _DURATION_PATTERN + r"(?:内|之内)?", "surgery"),
        (r"入院(前|后)\s*" + _DURATION_PATTERN + r"(?:内|之内)?", "admission"),
        (r"出院(前|后)\s*" + _DURATION_PATTERN + r"(?:内|之内)?", "discharge"),
    )
    for pattern, event in event_patterns:
        match = re.search(pattern, text)
        if match:
            relation = "before" if match.group(1) == "前" else "after"
            return TemporalIR(
                scope="event_window",
                event=event,
                relation=relation,
                duration=_duration_value(match.group(2)),
                unit=match.group(3),
                selection=_event_selection(text),
                raw=match.group(0),
            )

    trailing_event_patterns = (
        (r"(?:手术|术)\s*" + _DURATION_PATTERN + r"(前|后)(?:内|之内)?", "surgery"),
        (r"入院\s*" + _DURATION_PATTERN + r"(前|后)(?:内|之内)?", "admission"),
        (r"出院\s*" + _DURATION_PATTERN + r"(前|后)(?:内|之内)?", "discharge"),
    )
    for pattern, event in trailing_event_patterns:
        match = re.search(pattern, text)
        if match:
            return TemporalIR(
                scope="event_window",
                event=event,
                relation="before" if match.group(3) == "前" else "after",
                duration=_duration_value(match.group(1)),
                unit=match.group(2),
                selection=_event_selection(text),
                raw=match.group(0),
            )

    # Chinese commonly omits "after" in admission/discharge windows. Surgery
    # remains unresolved without an explicit direction because both are valid.
    implicit_after = re.search(r"(入院|出院)\s*" + _DURATION_PATTERN + r"(?:内|之内)", text)
    if implicit_after:
        return TemporalIR(
            scope="event_window",
            event="admission" if implicit_after.group(1) == "入院" else "discharge",
            relation="after",
            duration=_duration_value(implicit_after.group(2)),
            unit=implicit_after.group(3),
            selection=_event_selection(text),
            raw=implicit_after.group(0),
        )

    event_relation = re.search(r"(手术|术|入院|出院)\s*(前|后)", text)
    if event_relation:
        event = {
            "手术": "surgery",
            "术": "surgery",
            "入院": "admission",
            "出院": "discharge",
        }[event_relation.group(1)]
        return TemporalIR(
            scope="event_relation",
            event=event,
            relation="before" if event_relation.group(2) == "前" else "after",
            selection=_event_selection(text),
            raw=event_relation.group(0),
        )

    if "住院期间" in text or "本次住院" in text:
        return TemporalIR(scope="encounter", event="encounter", relation="during", raw="住院期间")

    recent = re.search(r"最近\s*" + _DURATION_PATTERN + r"(?:内|之内)?", text)
    if recent:
        return TemporalIR(
            scope="relative",
            relation="recent",
            duration=_duration_value(recent.group(1)),
            unit=recent.group(2),
            raw=recent.group(0),
        )
    return None


def _event_selection(text: str) -> str:
    if re.search(r"首次|第一次", text):
        return "first"
    if re.search(r"末次|最后一次|最近一次", text):
        return "last"
    if re.search(r"任意一次|任何一次", text):
        return "any"
    return "unspecified"


def _parse_assertion_ir(text: str, supplied: object = None) -> AssertionIR:
    data = _coerce_mapping(supplied)
    if data:
        return AssertionIR(
            present=_optional_bool(data.get("present")),
            certainty=str(data.get("certainty") or ""),
            subject=str(data.get("subject") or "patient"),
            temporal_context=str(data.get("temporal_context") or ""),
        )

    negated = bool(re.search(r"否认|未见|不存在|无明显|排除", text))
    certainty = "suspected" if re.search(r"疑似|考虑|可能|待排", text) else "confirmed"
    subject = "family" if re.search(r"家族史|父亲|母亲|家属", text) else "patient"
    temporal_context = "history" if re.search(r"既往|病史", text) else "current"
    return AssertionIR(
        present=False if negated else True,
        certainty=certainty,
        subject=subject,
        temporal_context=temporal_context,
    )


def _parse_quantifier_ir(text: str, supplied: object = None) -> Optional[QuantifierIR]:
    data = _coerce_mapping(supplied)
    if data:
        return QuantifierIR(
            mode=str(data.get("mode") or ""),
            count=_optional_number(data.get("count")),
            unit=str(data.get("unit") or "次"),
        )

    count_match = re.search(r"(至少|不低于|超过|多于|恰好)?\s*([0-9]+|[一二两三四五六七八九十百]+)\s*次", text)
    if count_match:
        prefix = count_match.group(1) or ""
        mode = "at_least" if prefix in {"至少", "不低于"} else "more_than" if prefix in {"超过", "多于"} else "exact"
        return QuantifierIR(mode=mode, count=_duration_value(count_match.group(2)), unit="次")
    if "连续" in text:
        return QuantifierIR(mode="consecutive", unit="次")
    if re.search(r"首次|第一次", text):
        return QuantifierIR(mode="first", count=1, unit="次")
    if re.search(r"末次|最后一次", text):
        return QuantifierIR(mode="last", count=1, unit="次")
    if re.search(r"任意一次|任何一次", text):
        return QuantifierIR(mode="any", count=1, unit="次")
    return None


def build_query_ir(analysis: dict, original_condition: str) -> QueryIR:
    conditions = []
    for index, cond in enumerate(analysis.get("conditions", []) or []):
        if not isinstance(cond, dict):
            continue
        text = cond.get("text") or original_condition
        has_explicit_numeric_predicate = bool(re.search(
            r"(>=|<=|>|<|=|\u2265|\u2264|\uff1e|\uff1c|\u5927\u4e8e|\u5c0f\u4e8e|\u9ad8\u4e8e|\u4f4e\u4e8e|\u8d85\u8fc7|\u4e0d\u5c11\u4e8e|\u4e0d\u4f4e\u4e8e|\u4e0d\u8d85\u8fc7|\u81f3\u591a|\u81f3\u5c11|\u7b49\u4e8e|\u4ee5\u4e0a|\u4ee5\u4e0b)",
            text or "",
        ))
        cmp_info = (
            parse_numeric_comparison(text)
            if has_explicit_numeric_predicate
            else None
        )
        has_explicit_predicate = bool(re.search(
            r"(>|<|>=|<=|=|≥|≤|＞|＜|大于|小于|高于|低于|超过|不少于|不低于|不超过|至多|至少|等于|以上|以下|偏高|偏低|异常|正常)",
            text or "",
        ))
        temporal = _parse_temporal_ir(text, cond.get("temporal"))
        depends_on = [
            str(item).strip()
            for item in (cond.get("depends_on", []) or [])
            if str(item).strip()
        ]
        if temporal and temporal.event:
            event_dependency = f"event:{temporal.event}"
            if event_dependency not in depends_on:
                depends_on.append(event_dependency)

        attributes = dict(cond.get("attributes") or {})
        if cond.get("predicate") and "predicate" not in attributes:
            attributes["predicate"] = cond["predicate"]

        conditions.append(
            ConditionIR(
                text=text,
                condition_id=str(cond.get("condition_id") or f"c{index + 1}"),
                domain=_infer_domain(cond, text),
                keyword=cond.get("keyword", "") or text,
                entity=cond.get("entity", "") or cond.get("keyword", "") or text,
                entity_type=cond.get("entity_type", ""),
                predicate=cond.get("predicate", ""),
                modifiers=list(cond.get("modifiers", []) or []),
                target_docs=list(cond.get("target_docs", []) or []),
                target_sections=list(cond.get("target_sections", []) or []),
                target_services=list(cond.get("target_skills") or cond.get("target_services") or []),
                is_numeric=bool(cmp_info and has_explicit_numeric_predicate),
                semantic_class=cond.get("semantic_class", ""),
                numeric_comparison=cmp_info.to_dict() if (cmp_info and has_explicit_predicate) else None,
                temporal=temporal,
                assertion=_parse_assertion_ir(text, cond.get("assertion")),
                quantifier=_parse_quantifier_ir(text, cond.get("quantifier")),
                depends_on=depends_on,
                attributes=attributes,
            )
        )
    if not conditions:
        temporal = _parse_temporal_ir(original_condition)
        conditions.append(
            ConditionIR(
                text=original_condition,
                condition_id="c1",
                domain=_infer_domain({}, original_condition),
                keyword=original_condition,
                temporal=temporal,
                assertion=_parse_assertion_ir(original_condition),
                quantifier=_parse_quantifier_ir(original_condition),
                depends_on=[f"event:{temporal.event}"] if temporal and temporal.event else [],
            )
        )

    return QueryIR(
        original=original_condition,
        query_type=analysis.get("type", "simple"),
        connector=analysis.get("connector"),
        negated=bool(analysis.get("negated", False)),
        source=analysis.get("source", ""),
        conditions=conditions,
    )
