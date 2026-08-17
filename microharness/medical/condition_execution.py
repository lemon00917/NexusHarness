"""Stable executor input derived from Query IR and EvidencePlan output."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Callable, Optional

from .query_ir import AssertionIR, ConditionIR, QuantifierIR, QueryIR, TemporalIR
from .semantic_rules import (
    OUTCOME_CLASS,
    PRE_ADMISSION_CLASS,
    extract_outcome_keyword,
    extract_outcome_modifiers,
    extract_outcome_phase,
    is_pre_admission_condition,
    normalize_outcome_phase,
    normalize_outcome_state,
)
from .temporal_parser import (
    compare_values,
    convert_numeric_unit,
    normalize_time_unit,
    operator_display,
)


_DURATION_UNITS = {"分钟", "小时", "天", "周", "月"}


def validate_numeric_comparison(
    comparison: object,
    *,
    required: bool,
    is_age_condition: bool,
) -> str:
    """Validate the structured comparison shared by all numeric executors."""
    if not required:
        return ""
    if not isinstance(comparison, dict):
        return "缺少结构化数值比较条件"
    if not str(comparison.get("subject") or "").strip():
        return "数值比较缺少比较主体"
    operator = str(comparison.get("operator") or comparison.get("op") or "").strip()
    if not operator or compare_values(0.0, operator, 0.0) is None:
        return "数值比较缺少有效比较符"
    threshold = comparison.get("threshold")
    if isinstance(threshold, bool):
        return "数值比较缺少有效阈值"
    try:
        float(threshold)
    except (TypeError, ValueError):
        return "数值比较缺少有效阈值"
    if is_age_condition and normalize_time_unit(
        str(comparison.get("unit") or "").strip()
    ) != "岁":
        return "年龄比较缺少单位“岁”"
    return ""


def _unique_strings(values: object) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple, set)):
        return ()
    return tuple(dict.fromkeys(
        str(value).strip() for value in values if str(value).strip()
    ))


def _targets_tuple(value: object) -> tuple[tuple[str, tuple[str, ...]], ...]:
    if not isinstance(value, dict):
        return ()
    return tuple(
        (str(document).strip(), _unique_strings(sections))
        for document, sections in value.items()
        if str(document).strip()
    )


def _attribute_text(attributes: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = attributes.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _structured_outcome_phase(
    attributes: dict[str, Any],
    temporal: Optional[TemporalIR],
) -> str:
    phase = normalize_outcome_phase(_attribute_text(
        attributes,
        "outcome_phase",
        "clinical_phase",
        "phase",
    ))
    if phase or temporal is None:
        return phase

    event = str(temporal.event or "").strip().lower()
    relation = str(temporal.relation or "").strip().lower()
    if event == "discharge":
        return "post_discharge" if relation == "after" else "discharge"
    if event in {"surgery", "operation"} and relation == "after":
        return "postoperative"
    if event in {"encounter", "hospitalization", "inpatient"}:
        return "hospitalization"
    if event == "admission":
        return "admission"
    return ""


def _has_internal_negation(
    assertion: Optional[AssertionIR],
    modifiers: tuple[str, ...],
) -> bool:
    if assertion is not None and assertion.present is False:
        return True
    return any(
        any(token in modifier for token in ("没有", "无", "不", "未", "没"))
        for modifier in modifiers
    )


@dataclass(frozen=True)
class ConditionExecutionSpec:
    """Immutable condition input used by the execution stage."""

    execution_key: str
    position: int
    condition_id: str
    text: str
    domain: str = ""
    keyword: str = ""
    entity: str = ""
    canonical_entity: str = ""
    aliases: tuple[str, ...] = ()
    entity_candidates: tuple[str, ...] = ()
    entity_confidence: Optional[float] = None
    normalization_source: str = ""
    entity_type: str = ""
    predicate: str = ""
    semantic_class: str = ""
    modifiers: tuple[str, ...] = ()
    numeric_comparison: Optional[dict[str, Any]] = None
    is_numeric: bool = False
    temporal: Optional[TemporalIR] = None
    assertion: Optional[AssertionIR] = None
    quantifier: Optional[QuantifierIR] = None
    depends_on: tuple[str, ...] = ()
    attributes: dict[str, Any] = field(default_factory=dict)
    target_docs: tuple[str, ...] = ()
    target_sections: tuple[str, ...] = ()
    target_services: tuple[str, ...] = ()
    targets: tuple[tuple[str, tuple[str, ...]], ...] = ()
    evidence_plan_source_ids: tuple[str, ...] = ()
    outcome_state: str = ""
    outcome_phase: str = ""
    is_outcome_condition: bool = False
    history_context: bool = False
    internal_negation: bool = False
    diagnosis_phase_evidence_allowed: bool = False
    execution_source: str = "ir"
    legacy_fallback_allowed: bool = False

    def targets_dict(self) -> dict[str, list[str]]:
        return {document: list(sections) for document, sections in self.targets}

    @property
    def numeric_execution_required(self) -> bool:
        return bool(
            self.is_numeric
            or self.numeric_comparison
            or str(self.predicate or "").strip().lower() == "compare"
        )

    @property
    def is_age_condition(self) -> bool:
        comparison = self.numeric_comparison or {}
        subject = str(comparison.get("subject") or "").strip()
        unit = normalize_time_unit(str(comparison.get("unit") or "").strip())
        return bool(
            self.numeric_execution_required
            and (
                str(self.domain or "").strip().lower() == "demographic"
                or str(self.entity_type or "").strip().lower() in {"age", "demographic"}
                or subject == "年龄"
                or unit == "岁"
            )
        )

    def numeric_comparison_issue(self) -> str:
        return validate_numeric_comparison(
            self.numeric_comparison,
            required=self.numeric_execution_required,
            is_age_condition=self.is_age_condition,
        )

    def condition_dict(self) -> dict[str, Any]:
        """Compatibility shape for domain rules that still accept dictionaries."""
        return {
            "execution_key": self.execution_key,
            "position": self.position,
            "condition_id": self.condition_id,
            "text": self.text,
            "domain": self.domain,
            "keyword": self.keyword,
            "entity": self.entity,
            "canonical_entity": self.canonical_entity,
            "aliases": list(self.aliases),
            "entity_candidates": list(self.entity_candidates),
            "entity_confidence": self.entity_confidence,
            "normalization_source": self.normalization_source,
            "entity_type": self.entity_type,
            "predicate": self.predicate,
            "semantic_class": self.semantic_class,
            "modifiers": list(self.modifiers),
            "numeric_comparison": self.numeric_comparison,
            "is_numeric": self.is_numeric,
            "target_docs": list(self.target_docs),
            "target_sections": list(self.target_sections),
            "target_skills": list(self.target_services),
            "targets": self.targets_dict(),
            "evidence_plan_source_ids": list(self.evidence_plan_source_ids),
            "temporal": {
                "scope": self.temporal.scope,
                "event": self.temporal.event,
                "relation": self.temporal.relation,
                "duration": self.temporal.duration,
                "unit": self.temporal.unit,
                "selection": self.temporal.selection,
                "raw": self.temporal.raw,
            } if self.temporal else None,
            "assertion": {
                "present": self.assertion.present,
                "certainty": self.assertion.certainty,
                "subject": self.assertion.subject,
                "temporal_context": self.assertion.temporal_context,
            } if self.assertion else None,
            "quantifier": {
                "mode": self.quantifier.mode,
                "count": self.quantifier.count,
                "unit": self.quantifier.unit,
            } if self.quantifier else None,
            "depends_on": list(self.depends_on),
            "attributes": dict(self.attributes),
            "outcome_state": self.outcome_state,
            "outcome_phase": self.outcome_phase,
            "is_outcome_condition": self.is_outcome_condition,
            "history_context": self.history_context,
            "internal_negation": self.internal_negation,
            "diagnosis_phase_evidence_allowed": self.diagnosis_phase_evidence_allowed,
            "execution_source": self.execution_source,
            "legacy_fallback_allowed": self.legacy_fallback_allowed,
        }


def prejudge_numeric_hints(
    spec: ConditionExecutionSpec,
    hints: str,
) -> Optional[dict[str, Any]]:
    """Evaluate a complete IR comparison against deterministic field hints."""
    if spec.numeric_comparison_issue() or not hints:
        return None

    comparison = spec.numeric_comparison or {}
    subject = str(comparison.get("subject") or "").strip()
    operator = str(comparison.get("operator") or comparison.get("op") or "").strip()
    threshold = float(comparison["threshold"])
    comparison_unit = normalize_time_unit(str(comparison.get("unit") or "").strip())

    hint_values: dict[str, float] = {}
    hint_raw: dict[str, str] = {}
    for line in hints.splitlines():
        match = re.match(r"\[预计算\]\s+(.+?)\s*=\s*([+-]?\d+(?:\.\d+)?)", line)
        if not match:
            continue
        key = match.group(1).strip()
        try:
            hint_values[key] = float(match.group(2))
            hint_raw[key] = line
        except ValueError:
            continue
    if not hint_values:
        return None

    def hint_unit(key: str) -> str:
        match = re.search(
            r"=\s*[+-]?\d+(?:\.\d+)?\s*(天|小时|分钟|岁|个|次|度|%)?",
            hint_raw.get(key, ""),
        )
        return normalize_time_unit(match.group(1) if match and match.group(1) else "")

    def threshold_for_hint(key: str) -> Optional[float]:
        source_unit = comparison_unit
        target_unit = hint_unit(key)
        if source_unit == target_unit:
            return threshold
        if source_unit in _DURATION_UNITS and target_unit in _DURATION_UNITS:
            return convert_numeric_unit(threshold, source_unit, target_unit)
        return None

    def result_for(key: str, value: float, *, approximate: bool = False) -> Optional[dict[str, Any]]:
        converted_threshold = threshold_for_hint(key)
        if converted_threshold is None:
            return None
        matched = compare_values(value, operator, converted_threshold)
        if matched is None:
            return None
        relation = "≈" if approximate else "="
        reason = (
            f"{subject} {relation} {key} = {value} "
            f"{operator_display(operator)} {converted_threshold} → "
            f"{'✓符合' if matched else '✗不符合'}"
        )
        if not approximate and key == subject:
            reason = (
                f"{subject} = {value} {operator_display(operator)} "
                f"{converted_threshold} → {'✓符合' if matched else '✗不符合'}"
            )
        return {"matched": matched, "reason": reason}

    for key, value in hint_values.items():
        if subject in key or key in subject:
            result = result_for(key, value, approximate=key != subject)
            if result is not None:
                return result

    if comparison_unit:
        same_unit = [
            key for key in hint_values
            if hint_unit(key) == comparison_unit
        ]
        if len(same_unit) == 1:
            key = same_unit[0]
            result = result_for(key, hint_values[key], approximate=True)
            if result is not None:
                return result

        if comparison_unit in _DURATION_UNITS:
            preferred_unit = "小时" if comparison_unit in {"分钟", "小时"} else "天"
            compatible = [
                key for key in hint_values
                if hint_unit(key) == preferred_unit
            ]
            if len(compatible) == 1:
                key = compatible[0]
                return result_for(key, hint_values[key], approximate=True)

    return None


def _raw_condition_for(
    condition: ConditionIR,
    index: int,
    raw_conditions: list[dict[str, Any]],
) -> dict[str, Any]:
    for raw_condition in raw_conditions:
        if str(raw_condition.get("condition_id") or "") == condition.condition_id:
            return raw_condition
    return raw_conditions[index] if index < len(raw_conditions) else {}


def _build_spec(
    condition: ConditionIR,
    raw_condition: dict[str, Any],
    *,
    condition_id: str,
    execution_key: str,
    position: int,
    keyword: str,
    primary_entity: str,
    candidates: tuple[str, ...],
    modifiers: tuple[str, ...],
    outcome_state: str,
    outcome_phase: str,
    is_outcome_condition: bool,
    history_context: bool,
    internal_negation: bool,
    diagnosis_phase_evidence_allowed: bool,
    legacy_source: bool,
) -> ConditionExecutionSpec:
    return ConditionExecutionSpec(
        execution_key=execution_key,
        position=position,
        condition_id=condition_id,
        text=str(condition.text or "").strip(),
        domain=condition.domain,
        keyword=keyword,
        entity=condition.entity,
        canonical_entity=primary_entity,
        aliases=_unique_strings(condition.aliases),
        entity_candidates=candidates,
        entity_confidence=condition.entity_confidence,
        normalization_source=condition.normalization_source,
        entity_type=condition.entity_type,
        predicate=condition.predicate,
        semantic_class=condition.semantic_class,
        modifiers=modifiers,
        numeric_comparison=condition.numeric_comparison,
        is_numeric=condition.is_numeric,
        temporal=condition.temporal,
        assertion=condition.assertion,
        quantifier=condition.quantifier,
        depends_on=_unique_strings(condition.depends_on),
        attributes=dict(condition.attributes),
        target_docs=_unique_strings(condition.target_docs),
        target_sections=_unique_strings(condition.target_sections),
        target_services=_unique_strings(condition.target_services),
        targets=_targets_tuple(raw_condition.get("targets")),
        evidence_plan_source_ids=_unique_strings(
            raw_condition.get("evidence_plan_source_ids")
        ),
        outcome_state=outcome_state,
        outcome_phase=outcome_phase,
        is_outcome_condition=is_outcome_condition,
        history_context=history_context,
        internal_negation=internal_negation,
        diagnosis_phase_evidence_allowed=diagnosis_phase_evidence_allowed,
        execution_source="legacy_fallback" if legacy_source else "ir",
        legacy_fallback_allowed=legacy_source,
    )


def build_condition_execution_specs(
    query_ir: QueryIR,
    analysis: dict[str, Any],
    *,
    fallback_keyword_fn: Optional[Callable[[str], str]] = None,
) -> list[ConditionExecutionSpec]:
    """Build position-stable executor inputs from the final Query IR."""
    raw_conditions = [
        item for item in (analysis.get("conditions") or []) if isinstance(item, dict)
    ]
    legacy_source = "fallback" in str(query_ir.source or "").lower()
    specs: list[ConditionExecutionSpec] = []

    for index, condition in enumerate(query_ir.conditions):
        raw_condition = _raw_condition_for(condition, index, raw_conditions)
        keyword = str(condition.keyword or condition.text).strip()
        if legacy_source and fallback_keyword_fn is not None:
            keyword = str(fallback_keyword_fn(keyword) or keyword).strip()

        modifiers = _unique_strings(condition.modifiers)
        if legacy_source:
            legacy_outcome_modifiers = _unique_strings(
                extract_outcome_modifiers(condition.text)
            )
            modifiers = _unique_strings((*modifiers, *legacy_outcome_modifiers))
            if legacy_outcome_modifiers:
                keyword = str(
                    extract_outcome_keyword(
                        condition.text,
                        fallback_keyword_fn=fallback_keyword_fn,
                    )
                    or keyword
                ).strip()

        if legacy_source:
            # build_query_ir fills missing entity fields with the full condition
            # text. Legacy fallback must prefer its cleaned keyword instead.
            primary_entity = str(
                keyword
                or condition.canonical_entity
                or condition.entity
                or condition.text
            ).strip()
        else:
            primary_entity = str(
                condition.canonical_entity
                or condition.entity
                or keyword
                or condition.text
            ).strip()
        candidate_input = {
            **raw_condition,
            "canonical_entity": primary_entity,
            "entity": condition.entity,
            "keyword": keyword,
            "aliases": list(condition.aliases),
            "entity_candidates": list(condition.entity_candidates),
        }
        from .entity_normalization import entity_candidates as build_entity_candidates

        candidates = tuple(build_entity_candidates(primary_entity, candidate_input))
        attributes = dict(condition.attributes)
        outcome_state = normalize_outcome_state(
            _attribute_text(attributes, "outcome_state", "state") or modifiers
        )
        outcome_phase = _structured_outcome_phase(attributes, condition.temporal)
        if legacy_source and not outcome_phase:
            outcome_phase = extract_outcome_phase(condition.text)
        is_outcome_condition = bool(
            outcome_state
            or str(condition.predicate or "").lower() == "outcome"
            or str(condition.entity_type or "").lower() == "outcome"
            or condition.semantic_class == OUTCOME_CLASS.name
        )
        history_context = bool(
            condition.semantic_class == PRE_ADMISSION_CLASS.name
            or (
                condition.assertion is not None
                and str(condition.assertion.temporal_context or "").lower()
                in {"history", "historical", "prior", "pre_admission"}
            )
            or (legacy_source and is_pre_admission_condition(condition.text))
        )
        internal_negation = bool(
            _has_internal_negation(condition.assertion, modifiers)
            or outcome_state == "not_improved"
        )
        evidence_kind = _attribute_text(
            attributes,
            "outcome_evidence",
            "evidence_kind",
        ).lower()
        diagnosis_phase_evidence_allowed = bool(
            outcome_phase == "discharge"
            and (
                str(condition.predicate or "").lower() == "diagnosed"
                or evidence_kind in {"diagnosis", "discharge_diagnosis"}
                or (
                    legacy_source
                    and any(
                        token in condition.text
                        for token in ("出院诊断", "仍诊断", "仍为", "仍是", "仍有")
                    )
                )
            )
        )
        condition_id = condition.condition_id or f"c{index + 1}"
        specs.append(_build_spec(
            condition,
            raw_condition,
            condition_id=condition_id,
            execution_key=f"{condition_id}@{index + 1}",
            position=index + 1,
            keyword=keyword,
            primary_entity=primary_entity,
            candidates=candidates,
            modifiers=modifiers,
            outcome_state=outcome_state,
            outcome_phase=outcome_phase,
            is_outcome_condition=is_outcome_condition,
            history_context=history_context,
            internal_negation=internal_negation,
            diagnosis_phase_evidence_allowed=diagnosis_phase_evidence_allowed,
            legacy_source=legacy_source,
        ))

    return specs
