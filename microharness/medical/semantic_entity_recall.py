"""Validate LLM-proposed semantic entity mentions against source evidence.

The language model is limited to verbatim candidate extraction and a separate
strict-equivalence audit. Final assertion, subject, negation, certainty, and
temporal decisions remain deterministic.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

from microharness.medical.document_semantics import (
    MATCHED,
    NOT_MATCHED,
    NOT_MENTIONED,
    UNKNOWN,
    DocumentSemanticDecision,
    assess_document_semantics,
)
from microharness.medical.time_window import TimeWindow


EXACT = "EXACT"
STRICT_EQUIVALENT = "STRICT_EQUIVALENT"
CLINICAL_ENTAILMENT = "CLINICAL_ENTAILMENT"
RELATED = "RELATED"
BROADER = "BROADER"
NARROWER = "NARROWER"
NONE = "NONE"
UNCERTAIN = "UNCERTAIN"

_ALLOWED_RELATIONS = {
    EXACT,
    STRICT_EQUIVALENT,
    CLINICAL_ENTAILMENT,
    RELATED,
    BROADER,
    NARROWER,
    NONE,
    UNCERTAIN,
}
_NON_EQUIVALENT_RELATIONS = {RELATED, BROADER, NARROWER, NONE}
_ENTAILING_RELATIONS = {"SAME_CONCEPT", "SOURCE_MORE_SPECIFIC"}
_REJECTED_RELATIONS = {"RELATED_ONLY", "SOURCE_BROADER", "UNRELATED"}
_AUDIT_RELATIONS = _ENTAILING_RELATIONS | _REJECTED_RELATIONS | {"UNCERTAIN"}
_SYMPTOM_REVIEW_RELATIONS = {
    "SAME_SYMPTOM",
    "SOURCE_QUALIFIED_SAME_SYMPTOM",
    "DISTINCT_SYMPTOMS",
    "UNCERTAIN",
}
_ASSERTION_KINDS = {
    "DIAGNOSIS",
    "SYMPTOM_OR_SIGN",
    "OBSERVATION_OR_MEASUREMENT",
    "MEDICATION",
    "LAB_TEST",
    "PROCEDURE",
    "OTHER",
}
MAX_SEMANTIC_CANDIDATES = 5
SEMANTIC_CANDIDATE_SOURCE_LIMIT = 1500


@dataclass(frozen=True)
class SemanticCandidateBatch:
    candidates: tuple[dict, ...]
    complete: bool
    overflow: bool = False
    valid: bool = True
    reason: str = ''


def _key(value: Any) -> str:
    return re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", str(value or "")).lower()


def _candidate_keys(query_entity: str, candidates: Iterable[str] | None) -> set[str]:
    values = [query_entity, *(candidates or [])]
    return {key for value in values if (key := _key(value))}


def _coerce_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
    return None


def _candidate_found(payload: dict) -> bool | None:
    if "candidate_found" in payload:
        return _coerce_bool(payload.get("candidate_found"))
    return _coerce_bool(payload.get("entity_mentioned"))


def _assertion_kind(payload: dict, field: str, legacy_field: str) -> str:
    value = payload.get(field)
    if value is None:
        value = payload.get(legacy_field)
    normalized = str(value or "").strip().upper()
    return normalized if normalized in _ASSERTION_KINDS else ""


def _unknown(reason: str, reason_code: str, evidence: str = "") -> DocumentSemanticDecision:
    return DocumentSemanticDecision(
        UNKNOWN,
        reason,
        reason_code,
        evidence,
        trace=({"stage": "semantic_entity_recall", "accepted": False, "reason": reason_code},),
    )


def _not_mentioned(query_entity: str, reason: str = "", *, trace: dict | None = None) -> DocumentSemanticDecision:
    return DocumentSemanticDecision(
        NOT_MENTIONED,
        reason or f"文档未提及能够直接证明'{query_entity}'的医学实体",
        "SEMANTIC_ENTITY_NOT_MENTIONED",
        trace=({"stage": "semantic_entity_recall", "accepted": False, **(trace or {})},),
    )


def parse_semantic_candidate_batch(
    payload: dict,
    *,
    max_candidates: int = MAX_SEMANTIC_CANDIDATES,
) -> SemanticCandidateBatch:
    '''Normalize legacy single-candidate and new multi-candidate responses.'''
    if not isinstance(payload, dict):
        return SemanticCandidateBatch(
            (), False, valid=False,
            reason='\u8bed\u4e49\u53ec\u56de\u7ed3\u679c\u4e0d\u662fJSON\u5bf9\u8c61',
        )
    if 'candidates' not in payload:
        return SemanticCandidateBatch((payload,), True)
    raw_candidates = payload.get('candidates')
    if not isinstance(raw_candidates, list):
        return SemanticCandidateBatch(
            (), False, valid=False,
            reason='candidates\u5b57\u6bb5\u4e0d\u662f\u6570\u7ec4',
        )
    return _parse_semantic_candidate_list(payload, raw_candidates, max_candidates)


def _parse_semantic_candidate_list(
    payload: dict,
    raw_candidates: list,
    max_candidates: int,
) -> SemanticCandidateBatch:
    candidates: list[dict] = []
    seen: set[tuple[str, str]] = set()
    invalid_items = 0
    for item in raw_candidates[:max_candidates]:
        if not isinstance(item, dict):
            invalid_items += 1
            continue
        normalized = dict(item)
        normalized['candidate_found'] = True
        identity = (
            str(normalized.get('matched_entity') or '').strip(),
            str(normalized.get('evidence_span') or '').strip(),
        )
        if identity in seen:
            continue
        seen.add(identity)
        candidates.append(normalized)

    overflow = len(raw_candidates) > max_candidates
    search_complete = _coerce_bool(payload.get('search_complete'))
    declared_found = _candidate_found(payload)
    valid = invalid_items == 0 and not (
        not raw_candidates and declared_found is True
    )
    complete = search_complete is True and not overflow and valid
    if not raw_candidates and declared_found is False and search_complete is None:
        complete = True

    reason_parts = []
    if invalid_items:
        reason_parts.append(f'{invalid_items}\u4e2a\u5019\u9009\u683c\u5f0f\u65e0\u6548')
    if overflow:
        reason_parts.append(f'\u5019\u9009\u8d85\u8fc7\u4e0a\u9650{max_candidates}\u4e2a')
    if search_complete is not True and not (
        not raw_candidates and declared_found is False
    ):
        reason_parts.append('\u6a21\u578b\u672a\u786e\u8ba4\u5019\u9009\u641c\u7d22\u5b8c\u6574')
    return SemanticCandidateBatch(
        tuple(candidates), complete, overflow, valid, '\uff1b'.join(reason_parts)
    )


def semantic_candidate_retry_required(
    batch: SemanticCandidateBatch,
    source_text: str,
    *,
    source_limit: int = SEMANTIC_CANDIDATE_SOURCE_LIMIT,
    source_complete: bool = True,
) -> bool:
    """Return whether one strict completeness retry is safe and useful."""
    if not batch.valid or batch.complete or batch.overflow:
        return False
    if not source_complete:
        return False
    if len(source_text or "") > source_limit:
        return False
    return batch.reason == "\u6a21\u578b\u672a\u786e\u8ba4\u5019\u9009\u641c\u7d22\u5b8c\u6574"


def aggregate_semantic_entity_decisions(
    decisions: Iterable[DocumentSemanticDecision],
    *,
    query_entity: str,
    batch: SemanticCandidateBatch,
) -> DocumentSemanticDecision:
    '''Aggregate candidates deterministically; the LLM never votes on status.'''
    items = list(decisions)
    status_counts = {
        status: sum(item.status == status for item in items)
        for status in (MATCHED, NOT_MATCHED, NOT_MENTIONED, UNKNOWN)
    }
    aggregate_trace = {
        'stage': 'semantic_candidate_aggregation',
        'query_entity': query_entity,
        'candidate_count': len(items),
        'status_counts': status_counts,
        'complete': batch.complete,
        'overflow': batch.overflow,
        'valid': batch.valid,
        'reason': batch.reason,
    }
    candidate_traces = tuple(
        {
            'stage': 'semantic_candidate_result',
            'candidate_index': index,
            'status': item.status,
            'reason_code': item.reason_code,
            'reason': item.reason,
            'evidence': item.evidence,
        }
        for index, item in enumerate(items, start=1)
    )

    if not batch.valid and not status_counts[MATCHED]:
        detail = batch.reason or '\u65e0\u6cd5\u6821\u9a8c\u5019\u9009\u5217\u8868'
        return DocumentSemanticDecision(
            UNKNOWN,
            f'\u591a\u5019\u9009\u8bed\u4e49\u53ec\u56de\u7ed3\u679c\u4e0d\u5b8c\u6574\u6216\u683c\u5f0f\u65e0\u6548\uff1a{detail}',
            'SEMANTIC_CANDIDATE_BATCH_INVALID',
            trace=(aggregate_trace, *candidate_traces),
        )
    if not items:
        if batch.complete:
            return DocumentSemanticDecision(
                NOT_MENTIONED,
                f'\u5df2\u5b8c\u6574\u68c0\u67e5\u75c5\u5386\u539f\u6587\uff0c\u672a\u53d1\u73b0\u80fd\u591f\u76f4\u63a5\u8bc1\u660e\u0027{query_entity}\u0027\u7684\u533b\u5b66\u5b9e\u4f53\u5019\u9009',
                'SEMANTIC_ENTITY_NOT_MENTIONED',
                trace=(aggregate_trace,),
            )
        return DocumentSemanticDecision(
            UNKNOWN,
            f'\u672a\u53d6\u5f97\u53ef\u5ba1\u6838\u7684\u0027{query_entity}\u0027\u8bed\u4e49\u5019\u9009\uff0c\u4e14\u6a21\u578b\u672a\u786e\u8ba4\u5df2\u5b8c\u6574\u68c0\u67e5\u539f\u6587',
            'SEMANTIC_CANDIDATE_SEARCH_INCOMPLETE',
            trace=(aggregate_trace,),
        )

    selected = _select_semantic_candidate_decision(items, status_counts, batch, query_entity)
    reason = selected.reason
    if len(items) > 1:
        reason = f'\u5171\u6838\u5bf9{len(items)}\u4e2a\u539f\u6587\u8bed\u4e49\u5019\u9009\u3002{reason}'
    return DocumentSemanticDecision(
        selected.status,
        reason,
        selected.reason_code,
        selected.evidence,
        selected.categories,
        (aggregate_trace, *candidate_traces, *selected.trace),
    )


def _select_semantic_candidate_decision(
    items: list[DocumentSemanticDecision],
    status_counts: dict[str, int],
    batch: SemanticCandidateBatch,
    query_entity: str,
) -> DocumentSemanticDecision:
    if status_counts[MATCHED]:
        return next(item for item in items if item.status == MATCHED)
    if not batch.complete:
        detail = batch.reason or '\u5019\u9009\u641c\u7d22\u4e0d\u5b8c\u6574'
        return DocumentSemanticDecision(
            UNKNOWN,
            f'\u0027{query_entity}\u0027\u7684\u8bed\u4e49\u5019\u9009\u672a\u5b8c\u6210\u5168\u90e8\u5ba1\u6838\uff1a{detail}',
            'SEMANTIC_CANDIDATE_SEARCH_INCOMPLETE',
        )
    if status_counts[NOT_MATCHED]:
        return next(item for item in items if item.status == NOT_MATCHED)
    if status_counts[UNKNOWN]:
        return next(item for item in items if item.status == UNKNOWN)
    return next(item for item in items if item.status == NOT_MENTIONED)


def candidate_needing_equivalence(
    payload: dict,
    *,
    query_entity: str,
    entity_candidates: Iterable[str] | None,
    source_text: str,
) -> str:
    """Return the validated source entity when a second audit is required."""
    if not isinstance(payload, dict) or _candidate_found(payload) is not True:
        return ""
    matched_entity = str(payload.get("matched_entity") or "").strip()
    evidence_span = str(payload.get("evidence_span") or "").strip()
    if not matched_entity or not evidence_span:
        return ""
    if evidence_span not in source_text or matched_entity not in evidence_span:
        return ""
    if _key(matched_entity) in _candidate_keys(query_entity, entity_candidates):
        return ""
    return matched_entity


def symptom_relation_review_required(equivalence_payload: dict | None) -> bool:
    """Return whether an accepted symptom entailment needs a focused veto."""
    if not isinstance(equivalence_payload, dict):
        return False
    query_kind = _assertion_kind(equivalence_payload, "query_kind", "QUERY")
    source_kind = _assertion_kind(equivalence_payload, "source_kind", "SOURCE")
    relation = str(equivalence_payload.get("relation") or "").strip().upper()
    return (
        query_kind == "SYMPTOM_OR_SIGN"
        and source_kind == "SYMPTOM_OR_SIGN"
        and relation in _ENTAILING_RELATIONS
    )


def assess_semantic_entity_recall(
    payload: dict,
    *,
    query_entity: str,
    entity_candidates: Iterable[str] | None,
    source_text: str,
    equivalence_payload: dict | None = None,
    symptom_relation_payload: dict | None = None,
    condition: str = "",
    time_window: TimeWindow | None = None,
    record_time: datetime | None = None,
) -> DocumentSemanticDecision:
    """Validate candidate extraction and run deterministic document semantics.

    A source phrase that is not a known lexical candidate requires an
    independent one-way clinical-entailment audit. All four primitive decisions
    must be true; a model-provided summary label is deliberately ignored.
    """
    if not isinstance(payload, dict):
        return _unknown("语义召回结果格式无效，无法校验证据", "SEMANTIC_RECALL_INVALID_PAYLOAD")

    found = _candidate_found(payload)
    if found is None:
        return _unknown("语义召回未明确是否找到原文实体候选", "SEMANTIC_RECALL_MISSING_CANDIDATE_FLAG")

    relation = str(payload.get("semantic_relation") or "").strip().upper()
    if relation and relation not in _ALLOWED_RELATIONS:
        return _unknown("语义召回未返回有效的实体关系", "SEMANTIC_RECALL_INVALID_RELATION")
    if not found or relation in _NON_EQUIVALENT_RELATIONS:
        return _not_mentioned(query_entity, trace={"relation": relation or NONE})
    if relation == UNCERTAIN:
        return _unknown(
            f"文档中的候选表达是否能够直接证明'{query_entity}'仍不确定",
            "SEMANTIC_ENTITY_RELATION_UNCERTAIN",
        )

    matched_entity = str(payload.get("matched_entity") or "").strip()
    evidence_span = str(payload.get("evidence_span") or "").strip()
    if not matched_entity or not evidence_span:
        return _unknown("语义召回缺少命中实体或原文证据片段", "SEMANTIC_RECALL_EVIDENCE_MISSING")
    if evidence_span not in source_text:
        return _unknown(
            "语义召回返回的证据片段不在原始病历中，已拒绝该证据",
            "SEMANTIC_RECALL_NON_VERBATIM_EVIDENCE",
            evidence_span,
        )
    if matched_entity not in evidence_span:
        return _unknown(
            "语义召回的命中实体未出现在证据片段中，已拒绝该证据",
            "SEMANTIC_RECALL_ENTITY_OUTSIDE_EVIDENCE",
            evidence_span,
        )

    known_exact = _key(matched_entity) in _candidate_keys(query_entity, entity_candidates)
    if relation == EXACT and not known_exact:
        return _unknown(
            "语义召回将不同文本标记为精确匹配，已拒绝该证据",
            "SEMANTIC_RECALL_EXACT_MISMATCH",
            evidence_span,
        )

    accepted_relation = EXACT
    equivalence_trace: dict = {}
    symptom_relation_trace: dict = {}
    if not known_exact:
        if not isinstance(equivalence_payload, dict):
            return _unknown(
                "候选原文实体尚未完成独立临床蕴含审核",
                "SEMANTIC_EQUIVALENCE_REVIEW_MISSING",
                evidence_span,
            )
        query_kind = _assertion_kind(equivalence_payload, "query_kind", "QUERY")
        source_kind = _assertion_kind(equivalence_payload, "source_kind", "SOURCE")
        audit_relation = str(equivalence_payload.get("relation") or "").strip().upper()
        if not query_kind or not source_kind or audit_relation not in _AUDIT_RELATIONS:
            return _unknown(
                "严格临床蕴含审核字段不完整，无法安全采用该候选证据",
                "SEMANTIC_EQUIVALENCE_REVIEW_INVALID",
                evidence_span,
            )
        equivalence_trace = {
            "stage": "semantic_entailment_review",
            "query_entity": query_entity,
            "matched_entity": matched_entity,
            "query_kind": query_kind,
            "source_kind": source_kind,
            "relation": audit_relation,
            "reason": str(equivalence_payload.get("reason") or ""),
        }
        if query_kind == "OTHER" or source_kind == "OTHER":
            return _unknown(
                "严格临床蕴含审核无法确定查询或原文的断言层级",
                "SEMANTIC_EQUIVALENCE_REVIEW_INVALID",
                evidence_span,
            )
        if query_kind != source_kind:
            review_reason = str(equivalence_payload.get("reason") or "").strip()
            reason = (
                f"原文候选'{matched_entity}'与查询实体'{query_entity}'断言层级不兼容"
                f"（{source_kind} -> {query_kind}），不能跨层推断"
            )
            if review_reason:
                reason += f"：{review_reason}"
            return _not_mentioned(
                query_entity,
                reason,
                trace=equivalence_trace,
            )
        if audit_relation == "UNCERTAIN":
            return _unknown(
                "严格临床蕴含审核无法可靠确定原文与查询概念的关系",
                "SEMANTIC_ENTITY_RELATION_UNCERTAIN",
                evidence_span,
            )
        if audit_relation in _REJECTED_RELATIONS:
            review_reason = str(equivalence_payload.get("reason") or "").strip()
            reason = f"原文候选'{matched_entity}'不能单独证明查询实体'{query_entity}'"
            if review_reason:
                reason += f"：{review_reason}"
            return _not_mentioned(query_entity, reason, trace=equivalence_trace)
        if query_kind == "SYMPTOM_OR_SIGN" and source_kind == "SYMPTOM_OR_SIGN":
            if not isinstance(symptom_relation_payload, dict):
                return _unknown(
                    "症状候选尚未完成独立的症状同一性复核",
                    "SEMANTIC_SYMPTOM_RELATION_REVIEW_MISSING",
                    evidence_span,
                )
            symptom_relation = str(
                symptom_relation_payload.get("relation") or ""
            ).strip().upper()
            if symptom_relation not in _SYMPTOM_REVIEW_RELATIONS:
                return _unknown(
                    "症状同一性复核未返回有效枚举，无法安全采用该候选证据",
                    "SEMANTIC_SYMPTOM_RELATION_REVIEW_INVALID",
                    evidence_span,
                )
            symptom_relation_trace = {
                "stage": "symptom_relation_review",
                "query_entity": query_entity,
                "matched_entity": matched_entity,
                "relation": symptom_relation,
                "reason": str(symptom_relation_payload.get("reason") or ""),
            }
            if symptom_relation == "UNCERTAIN":
                return _unknown(
                    "症状同一性复核无法可靠确定两个症状是否为同一临床表现",
                    "SEMANTIC_SYMPTOM_RELATION_UNCERTAIN",
                    evidence_span,
                )
            if symptom_relation == "DISTINCT_SYMPTOMS":
                review_reason = str(
                    symptom_relation_payload.get("reason") or ""
                ).strip()
                reason = (
                    f"原文症状'{matched_entity}'与查询症状'{query_entity}'是不同的临床表现"
                )
                if review_reason:
                    reason += f"：{review_reason}"
                return _not_mentioned(
                    query_entity,
                    reason,
                    trace=symptom_relation_trace,
                )
        accepted_relation = CLINICAL_ENTAILMENT

    decision = assess_document_semantics(
        matched_entity,
        source_text,
        condition=condition,
        time_window=time_window,
        record_time=record_time,
    )
    trace = (
        {
            "stage": "semantic_entity_recall",
            "accepted": True,
            "query_entity": query_entity,
            "matched_entity": matched_entity,
            "relation": accepted_relation,
            "evidence_span": evidence_span,
        },
        *((equivalence_trace,) if equivalence_trace else ()),
        *((symptom_relation_trace,) if symptom_relation_trace else ()),
        *decision.trace,
    )
    reason = decision.reason
    if accepted_relation == CLINICAL_ENTAILMENT and _key(matched_entity) != _key(query_entity):
        reason = f"通过临床蕴含表达'{matched_entity}'核对'{query_entity}'：{reason}"
    return DocumentSemanticDecision(
        decision.status,
        reason,
        decision.reason_code,
        decision.evidence or evidence_span,
        decision.categories,
        trace,
    )


__all__ = [
    'MAX_SEMANTIC_CANDIDATES',
    'SEMANTIC_CANDIDATE_SOURCE_LIMIT',
    'SemanticCandidateBatch',
    'aggregate_semantic_entity_decisions',
    'parse_semantic_candidate_batch',
    'semantic_candidate_retry_required',
    "BROADER",
    "CLINICAL_ENTAILMENT",
    "EXACT",
    "NARROWER",
    "NONE",
    "RELATED",
    "STRICT_EQUIVALENT",
    "UNCERTAIN",
    "assess_semantic_entity_recall",
    "candidate_needing_equivalence",
    "symptom_relation_review_required",
]
