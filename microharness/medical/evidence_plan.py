"""Metadata-driven evidence planning contract.

This module normalizes routing candidates into a stable plan. It does not
execute queries or decide whether a patient matches.
"""

from __future__ import annotations

import copy
from dataclasses import asdict, dataclass, field
import re
from typing import Any

from microharness.medical.query_ir import QueryIR


_DOMAIN_EVIDENCE_ROLES = {
    "diagnosis": {"diagnosis_evidence", "disease_symptom_evidence"},
    "symptom": {"symptom_evidence", "disease_symptom_evidence"},
    "clinical_sign": {"clinical_sign_evidence", "symptom_evidence"},
    "clinical_concept": {"disease_symptom_evidence"},
    "laboratory": {"laboratory_evidence"},
    "medication": {"medication_evidence"},
    "procedure": {"procedure_evidence"},
    "encounter": {"encounter_evidence"},
    "demographic": {"demographic_evidence"},
    "imaging": {"imaging_evidence"},
}

_ROLE_KEYS = (
    "evidence_roles",
    "evidence_types",
    "document_role",
    "document_roles",
    "section_role",
    "section_roles",
)


@dataclass(frozen=True)
class EvidenceSourcePlan:
    source_id: str
    source_type: str
    requested_name: str
    resolved_name: str = ""
    sections: list[str] = field(default_factory=list)
    resolution_status: str = "resolved"
    reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ConditionEvidencePlan:
    condition_id: str
    condition_text: str
    domain: str
    sources: list[EvidenceSourcePlan] = field(default_factory=list)
    diagnostics: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "condition_id": self.condition_id,
            "condition_text": self.condition_text,
            "domain": self.domain,
            "sources": [source.to_dict() for source in self.sources],
            "diagnostics": list(self.diagnostics),
        }


@dataclass(frozen=True)
class EvidencePlan:
    original_condition: str
    conditions: list[ConditionEvidencePlan] = field(default_factory=list)
    version: str = "1.1"

    @property
    def unresolved_count(self) -> int:
        return sum(
            1
            for condition in self.conditions
            for source in condition.sources
            if source.resolution_status != "resolved"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "original_condition": self.original_condition,
            "conditions": [condition.to_dict() for condition in self.conditions],
            "unresolved_count": self.unresolved_count,
        }


def _normalize_name(value: object) -> str:
    text = str(value or "").strip()
    text = re.sub(r"^[<\[({\u300a\u300c\u300e\u3010]+|[>\])}\u300b\u300d\u300f\u3011]+$", "", text)
    return re.sub(r"\s+", "", text).lower()


def _as_list(value: object) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _normalized_values(value: object) -> set[str]:
    return {_normalize_name(item) for item in _as_list(value) if _normalize_name(item)}


def _metadata_roles(metadata: dict[str, Any]) -> set[str]:
    roles: set[str] = set()
    for key in _ROLE_KEYS:
        roles.update(_normalized_values(metadata.get(key)))
    semantic = metadata.get("semantic")
    if isinstance(semantic, dict):
        for key in (*_ROLE_KEYS, "domain", "domains", "entity_type", "entity_types"):
            roles.update(_normalized_values(semantic.get(key)))
    return roles


def _condition_roles(condition) -> set[str]:
    roles = {_normalize_name(role) for role in _DOMAIN_EVIDENCE_ROLES.get(condition.domain, set())}
    for key in _ROLE_KEYS:
        roles.update(_normalized_values(condition.attributes.get(key)))
    roles.update(_normalized_values(condition.attributes.get("evidence_role")))
    roles.add(_normalize_name(condition.domain))
    roles.add(_normalize_name(condition.entity_type))
    return {role for role in roles if role}


def _roles_match(desired: set[str], metadata: dict[str, Any]) -> bool:
    available = _metadata_roles(metadata)
    return bool(desired and available and desired.intersection(available))


def _section_items(document: dict[str, Any]) -> list[dict[str, Any]]:
    sections = document.get("sections") or []
    if isinstance(sections, dict):
        return [
            {"name": str(name), "purpose": purpose if isinstance(purpose, str) else ""}
            for name, purpose in sections.items()
        ]
    return [item for item in sections if isinstance(item, dict) and item.get("name")]


def _aliases(name: str, metadata: dict[str, Any]) -> set[str]:
    values = {name}
    for key in ("aliases", "alias", "name_aliases", "template_name", "table_name"):
        values.update(_as_list(metadata.get(key)))
    return {_normalize_name(value) for value in values if _normalize_name(value)}


def _resolve_catalog_name(requested: str, catalog: dict[str, dict]) -> tuple[str, str]:
    normalized = _normalize_name(requested)
    if not normalized:
        return "", "empty_name"

    matches = [name for name, metadata in catalog.items() if normalized in _aliases(name, metadata)]
    if len(matches) == 1:
        return matches[0], "metadata_name_or_alias"

    suffixes = ("document", "record", "form", "文档", "病历", "记录单", "表单", "表")
    variants = {normalized}
    for suffix in suffixes:
        if normalized.endswith(suffix):
            variants.add(normalized[: -len(suffix)])
    fuzzy = [
        name
        for name, metadata in catalog.items()
        if any(
            variant and (variant in alias or alias in variant)
            for variant in variants
            for alias in _aliases(name, metadata)
        )
    ]
    fuzzy = list(dict.fromkeys(fuzzy))
    if len(fuzzy) == 1:
        return fuzzy[0], "unique_fuzzy_name"
    return "", "ambiguous_name" if fuzzy else "unknown_name"


def _resolve_sections(requested: list[str], document: dict[str, Any]) -> tuple[list[str], list[str]]:
    items = _section_items(document)
    catalog = {str(item["name"]): item for item in items}
    resolved: list[str] = []
    unresolved: list[str] = []
    for section in requested:
        name, _ = _resolve_catalog_name(section, catalog)
        if name:
            if name not in resolved:
                resolved.append(name)
        else:
            unresolved.append(section)
    return resolved, unresolved


def build_evidence_plan(
    query_ir: QueryIR,
    document_catalog: dict[str, dict] | None = None,
    service_catalog: dict[str, dict] | None = None,
) -> EvidencePlan:
    """Build a non-executable, multi-source evidence plan from Query IR."""
    documents = document_catalog or {}
    services = {
        name: metadata
        for name, metadata in (service_catalog or {}).items()
        if name != "base_url" and isinstance(metadata, dict)
    }
    condition_plans: list[ConditionEvidencePlan] = []

    for condition in query_ir.conditions:
        sources: list[EvidenceSourcePlan] = []
        diagnostics: list[dict[str, Any]] = []
        source_keys: set[tuple[str, str]] = set()
        desired_roles = _condition_roles(condition)

        def add_source(source: EvidenceSourcePlan) -> None:
            key = (source.source_type, source.resolved_name or source.requested_name)
            if key not in source_keys:
                source_keys.add(key)
                sources.append(source)

        for requested_service in dict.fromkeys(condition.target_services):
            resolved, method = _resolve_catalog_name(requested_service, services)
            status = "resolved" if resolved else "unresolved"
            add_source(
                EvidenceSourcePlan(
                    source_id=f"{condition.condition_id}:service:{requested_service}",
                    source_type="service",
                    requested_name=requested_service,
                    resolved_name=resolved,
                    resolution_status=status,
                    reason=method,
                    metadata={"domain": condition.domain},
                )
            )
            if not resolved:
                diagnostics.append(
                    {
                        "code": "UNRESOLVED_SERVICE",
                        "requested_name": requested_service,
                        "reason": method,
                    }
                )

        for requested_document in dict.fromkeys(condition.target_docs):
            resolved, method = _resolve_catalog_name(requested_document, documents)
            if not resolved:
                add_source(
                    EvidenceSourcePlan(
                        source_id=f"{condition.condition_id}:document:{requested_document}",
                        source_type="document",
                        requested_name=requested_document,
                        resolution_status="unresolved",
                        reason=method,
                        metadata={"requested_sections": list(condition.target_sections)},
                    )
                )
                diagnostics.append(
                    {
                        "code": "UNRESOLVED_DOCUMENT",
                        "requested_name": requested_document,
                        "reason": method,
                    }
                )
                continue

            resolved_sections, unresolved_sections = _resolve_sections(
                condition.target_sections,
                documents[resolved],
            )
            status = "resolved" if not unresolved_sections else "partially_resolved"
            add_source(
                EvidenceSourcePlan(
                    source_id=f"{condition.condition_id}:document:{resolved}",
                    source_type="document",
                    requested_name=requested_document,
                    resolved_name=resolved,
                    sections=resolved_sections,
                    resolution_status=status,
                    reason=method,
                    metadata={
                        "purpose": str(documents[resolved].get("purpose") or ""),
                        "unresolved_sections": unresolved_sections,
                    },
                )
            )
            if unresolved_sections:
                diagnostics.append(
                    {
                        "code": "UNRESOLVED_SECTION",
                        "document": resolved,
                        "requested_names": unresolved_sections,
                    }
                )

        # Supplement explicit router choices with metadata roles. This is
        # domain-level routing and does not depend on concrete clinical names.
        for service_name, service_metadata in services.items():
            if not _roles_match(desired_roles, service_metadata):
                continue
            add_source(
                EvidenceSourcePlan(
                    source_id=f"{condition.condition_id}:service:{service_name}",
                    source_type="service",
                    requested_name=service_name,
                    resolved_name=service_name,
                    resolution_status="resolved",
                    reason="metadata_role_match",
                    metadata={
                        "domain": condition.domain,
                        "matched_roles": sorted(
                            desired_roles.intersection(_metadata_roles(service_metadata))
                        ),
                    },
                )
            )

        for document_name, document_metadata in documents.items():
            matched_sections = [
                str(section.get("name") or "").strip()
                for section in _section_items(document_metadata)
                if str(section.get("name") or "").strip()
                and _roles_match(desired_roles, section)
            ]
            document_match = _roles_match(desired_roles, document_metadata)
            if not matched_sections and not document_match:
                continue
            add_source(
                EvidenceSourcePlan(
                    source_id=f"{condition.condition_id}:document:{document_name}",
                    source_type="document",
                    requested_name=document_name,
                    resolved_name=document_name,
                    sections=list(dict.fromkeys(matched_sections)),
                    resolution_status="resolved",
                    reason="metadata_role_match",
                    metadata={
                        "purpose": str(document_metadata.get("purpose") or ""),
                        "matched_roles": sorted(
                            desired_roles.intersection(_metadata_roles(document_metadata))
                        ),
                    },
                )
            )

        if not sources:
            diagnostics.append(
                {
                    "code": "NO_EVIDENCE_SOURCE_PLANNED",
                    "condition_id": condition.condition_id,
                    "domain": condition.domain,
                }
            )

        condition_plans.append(
            ConditionEvidencePlan(
                condition_id=condition.condition_id,
                condition_text=condition.text,
                domain=condition.domain,
                sources=sources,
                diagnostics=diagnostics,
            )
        )

    return EvidencePlan(query_ir.original, condition_plans)


def apply_evidence_plan_to_analysis(analysis: dict, plan: EvidencePlan) -> dict:
    """Enrich existing executor inputs with resolved EvidencePlan sources."""
    enriched = copy.deepcopy(analysis or {})
    conditions = enriched.get("conditions")
    if not isinstance(conditions, list):
        return enriched

    plan_by_id = {item.condition_id: item for item in plan.conditions}
    for index, raw_condition in enumerate(conditions):
        if not isinstance(raw_condition, dict):
            continue
        condition_id = str(raw_condition.get("condition_id") or f"c{index + 1}")
        condition_plan = plan_by_id.get(condition_id)
        if condition_plan is None and index < len(plan.conditions):
            condition_plan = plan.conditions[index]
        if condition_plan is None:
            continue

        target_services = list(raw_condition.get("target_skills") or [])
        target_documents = list(raw_condition.get("target_docs") or [])
        target_sections = list(raw_condition.get("target_sections") or [])
        target_map = copy.deepcopy(raw_condition.get("targets") or {})
        if not isinstance(target_map, dict):
            target_map = {}
        consumed_source_ids: list[str] = []

        for source in condition_plan.sources:
            if source.resolution_status not in {"resolved", "partially_resolved"}:
                continue
            if not source.resolved_name:
                continue
            consumed_source_ids.append(source.source_id)
            if source.source_type == "service":
                if source.resolved_name not in target_services:
                    target_services.append(source.resolved_name)
            elif source.source_type == "document":
                if source.resolved_name not in target_documents:
                    target_documents.append(source.resolved_name)
                document_sections = list(target_map.get(source.resolved_name) or [])
                for section in source.sections:
                    if section not in target_sections:
                        target_sections.append(section)
                    if section not in document_sections:
                        document_sections.append(section)
                if document_sections:
                    target_map[source.resolved_name] = document_sections

        raw_condition["target_skills"] = target_services
        raw_condition["target_docs"] = target_documents
        raw_condition["target_sections"] = target_sections
        raw_condition["targets"] = target_map
        raw_condition["evidence_plan_source_ids"] = consumed_source_ids

    enriched["evidence_plan_version"] = plan.version
    return enriched
