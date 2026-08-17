#!/usr/bin/env python3
"""Evaluate medical filtering responses against reviewed gold assertions."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import statistics
import sys
import time
from typing import Any, Iterable
from urllib import error, request


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
DEFAULT_GOLD = ROOT / "evaluation" / "medical_filter" / "gold_cases.json"
DEFAULT_REPORT_DIR = ROOT / "evaluation" / "medical_filter" / "reports"

STATUS_MAP = {
    "MATCHED": "符合",
    "TRUE": "符合",
    "符合": "符合",
    "NOT_MATCHED": "不符合",
    "FALSE": "不符合",
    "不符合": "不符合",
    "NOT_MENTIONED": "未提及",
    "未提及": "未提及",
    "UNKNOWN": "无法判断",
    "无法判断": "无法判断",
    "不可判定": "无法判断",
}

DATA_UNAVAILABLE_MARKERS = (
    "未取得",
    "数据源不可用",
    "接口不可用",
    "接口失败",
    "连接失败",
    "连接超时",
    "请求超时",
    "缺少入院时间",
    "缺少出院时间",
    "缺少手术时间",
    "缺少事件时间",
    "缺少时间锚点",
    "缺少单位",
    "单位不兼容",
    "关键事实不足",
    "证据不足",
    "SOURCE_UNAVAILABLE",
    "MISSING_EVENT_TIME",
    "INSUFFICIENT_EVIDENCE",
)

ASSERTION_GROUPS = ("clinical", "routing", "evidence", "explanation")

REVIEW_STATUSES = ("pending", "routing_only", "verified", "rejected")
CLINICAL_EXPECTATION_FIELDS = (
    "overall_status",
    "condition_statuses",
    "overall_result",
)

LAYER_ORDER = (
    "understanding",
    "ir",
    "routing",
    "evidence",
    "temporal",
    "condition_adjudication",
    "overall_adjudication",
    "explanation",
)

LAYER_LABELS = {
    "understanding": "条件理解与拆分",
    "ir": "规范IR",
    "routing": "证据路由",
    "evidence": "证据召回",
    "temporal": "时间窗",
    "condition_adjudication": "子条件裁决",
    "overall_adjudication": "总体组合",
    "explanation": "解释一致性",
}


def _text_quality(text: str) -> tuple[int, int, int]:
    cjk = sum("\u3400" <= char <= "\u9fff" for char in text)
    replacement = text.count("\ufffd")
    suspicious = sum(char in "ÃÂäåæçèéêëìíîïðñòóôõöøùúûüýþ×¡ÔºÌÊýÐÇ±³Í´" for char in text)
    return cjk, -replacement, -suspicious


def repair_mojibake(value: str) -> str:
    """Repair common UTF-8/GBK text decoded as Latin-1 or Windows-1252."""
    candidates = [value]
    for source_encoding in ("latin1", "cp1252"):
        for target_encoding in ("utf-8", "gbk"):
            try:
                candidate = value.encode(source_encoding).decode(target_encoding)
            except (UnicodeEncodeError, UnicodeDecodeError):
                continue
            if candidate not in candidates:
                candidates.append(candidate)
    return max(candidates, key=_text_quality)


def canonicalize(value: Any) -> Any:
    if isinstance(value, str):
        return repair_mojibake(value)
    if isinstance(value, list):
        return [canonicalize(item) for item in value]
    if isinstance(value, dict):
        return {repair_mojibake(str(key)): canonicalize(item) for key, item in value.items()}
    return value


def load_json(path: Path) -> Any:
    return canonicalize(json.loads(path.read_text(encoding="utf-8-sig")))


def _has_selector(value: dict[str, Any], *names: str) -> bool:
    return any(str(value.get(name) or "").strip() for name in names)


def _valid_reviewed_at(value: Any) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _clinical_expectations(expected: dict[str, Any]) -> list[str]:
    return [name for name in CLINICAL_EXPECTATION_FIELDS if name in expected]


def validate_gold(gold: Any) -> list[str]:
    """Return schema errors that would make evaluation results misleading."""
    if not isinstance(gold, dict):
        return ["root: expected object"]
    cases = gold.get("cases")
    if not isinstance(cases, list):
        return ["cases: expected list"]

    errors: list[str] = []
    seen_ids: set[str] = set()
    object_lists = {
        "condition_ir",
        "condition_statuses",
        "routing_assertions",
        "evidence_assertions",
        "temporal_assertions",
        "explanation_audits",
    }
    string_lists = {
        "required_condition_contains",
        "required_services",
        "required_documents",
        "required_source_ids",
        "required_evidence_sources",
        "required_evidence_source_ids",
        "required_reason_contains",
        "forbidden_reason_contains",
    }

    for case_index, case in enumerate(cases):
        prefix = f"cases[{case_index}]"
        if not isinstance(case, dict):
            errors.append(f"{prefix}: expected object")
            continue

        case_id = str(case.get("id") or "").strip()
        if not case_id:
            errors.append(f"{prefix}.id: required non-empty string")
        elif case_id in seen_ids:
            errors.append(f"{prefix}.id: duplicate case id '{case_id}'")
        else:
            seen_ids.add(case_id)

        expected = case.get("expected")
        if not isinstance(expected, dict):
            errors.append(f"{prefix}.expected: expected object")
            continue

        review = case.get("review")
        if not isinstance(review, dict):
            errors.append(f"{prefix}.review: expected object")
            review = {}
        review_status = str(review.get("status") or "").strip().lower()
        if review_status not in REVIEW_STATUSES:
            errors.append(
                f"{prefix}.review.status: expected one of {', '.join(REVIEW_STATUSES)}"
            )
        clinical_fields = _clinical_expectations(expected)
        if clinical_fields and review_status != "verified":
            errors.append(
                f"{prefix}.review.status: verified is required for clinical expectations "
                f"{', '.join(clinical_fields)}"
            )
        if review_status == "verified":
            if not str(review.get("reviewed_by") or "").strip():
                errors.append(f"{prefix}.review.reviewed_by: required for verified review")
            if not _valid_reviewed_at(review.get("reviewed_at")):
                errors.append(
                    f"{prefix}.review.reviewed_at: valid ISO date/time required for verified review"
                )
        if review_status in {"routing_only", "verified", "rejected"} and not str(
            review.get("note") or ""
        ).strip():
            errors.append(f"{prefix}.review.note: required for {review_status} review")
        response_hash = str(
            review.get("source_response_sha256") or review.get("response_sha256") or ""
        ).strip().lower()
        if response_hash and (
            len(response_hash) != 64
            or any(char not in "0123456789abcdef" for char in response_hash)
        ):
            errors.append(
                f"{prefix}.review.source_response_sha256: expected 64 lowercase hex characters"
            )

        for name in object_lists:
            value = expected.get(name)
            if value is None:
                continue
            if not isinstance(value, list):
                errors.append(f"{prefix}.expected.{name}: expected list")
                continue
            for item_index, item in enumerate(value):
                if not isinstance(item, dict):
                    errors.append(
                        f"{prefix}.expected.{name}[{item_index}]: expected object"
                    )

        for name in string_lists:
            value = expected.get(name)
            if value is None:
                continue
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                errors.append(f"{prefix}.expected.{name}: expected string list")

        for name in ("normalization", "overall_result"):
            value = expected.get(name)
            if value is not None and not isinstance(value, dict):
                errors.append(f"{prefix}.expected.{name}: expected object")

        selector_lists = (
            "condition_ir",
            "condition_statuses",
            "routing_assertions",
            "evidence_assertions",
            "temporal_assertions",
        )
        for name in selector_lists:
            value = expected.get(name)
            if not isinstance(value, list):
                continue
            for item_index, item in enumerate(value):
                if not isinstance(item, dict):
                    continue
                item_prefix = f"{prefix}.expected.{name}[{item_index}]"
                if not _has_selector(item, "condition_id", "condition_contains"):
                    errors.append(
                        f"{item_prefix}: condition_id or condition_contains is required"
                    )
                if name == "routing_assertions" and not _has_selector(
                    item, "source_id", "source_contains"
                ):
                    errors.append(
                        f"{item_prefix}: source_id or source_contains is required"
                    )

        field_names = {
            "condition_ir": ("fields", "required_fields"),
            "routing_assertions": ("fields",),
            "evidence_assertions": ("fields_any",),
            "temporal_assertions": ("fields", "evidence_fields_any"),
            "explanation_audits": ("fields",),
        }
        for list_name, names in field_names.items():
            value = expected.get(list_name)
            if not isinstance(value, list):
                continue
            for item_index, item in enumerate(value):
                if not isinstance(item, dict):
                    continue
                for name in names:
                    if name in item and not isinstance(item[name], dict):
                        errors.append(
                            f"{prefix}.expected.{list_name}[{item_index}].{name}: "
                            "expected object"
                        )

        audits = expected.get("explanation_audits")
        if isinstance(audits, list):
            for item_index, item in enumerate(audits):
                if not isinstance(item, dict):
                    continue
                item_prefix = f"{prefix}.expected.explanation_audits[{item_index}]"
                scope = str(item.get("scope") or "overall").strip().lower()
                if scope not in {"overall", "condition", "source"}:
                    errors.append(f"{item_prefix}.scope: invalid scope '{scope}'")
                    continue
                if scope in {"condition", "source"} and not _has_selector(
                    item, "condition_id", "condition_contains"
                ):
                    errors.append(
                        f"{item_prefix}: condition_id or condition_contains is required"
                    )
                if scope == "source" and not _has_selector(
                    item, "source_id", "source_contains"
                ):
                    errors.append(
                        f"{item_prefix}: source_id or source_contains is required"
                    )

    return errors


def normalize_status(value: Any) -> str:
    if isinstance(value, bool):
        return "符合" if value else "不符合"
    text = repair_mojibake(str(value or "")).strip()
    return STATUS_MAP.get(text.upper(), STATUS_MAP.get(text, text))


def first_result(response: dict[str, Any]) -> dict[str, Any]:
    results = response.get("results") or []
    return results[0] if results and isinstance(results[0], dict) else {}


def canonical_overall_result(response: dict[str, Any]) -> dict[str, Any]:
    first = first_result(response)
    for value in (first.get("overall_result"), response.get("overall_result")):
        if isinstance(value, dict):
            return value
    return {}


def canonical_condition_results(response: dict[str, Any]) -> list[dict[str, Any]]:
    first = first_result(response)
    structured = first.get("condition_results") or []
    if isinstance(structured, list):
        items = [item for item in structured if isinstance(item, dict)]
        if items:
            return items

    items: list[dict[str, Any]] = []
    per_condition = first.get("per_condition") or {}
    if isinstance(per_condition, dict):
        for text, value in per_condition.items():
            if not isinstance(value, dict):
                continue
            canonical = value.get("condition_result")
            if isinstance(canonical, dict):
                item = dict(canonical)
                item.setdefault("condition", str(text))
                items.append(item)
    return items


def overall_status(response: dict[str, Any]) -> str:
    first = first_result(response)
    overall = canonical_overall_result(response)
    for value in (
        overall.get("status"),
        overall.get("判断状态"),
        response.get("判断状态"),
        first.get("判断状态"),
        first.get("status"),
    ):
        status = normalize_status(value)
        if status:
            return status
    if "matched" in first and first.get("可判定", first.get("conclusive", True)):
        return normalize_status(bool(first.get("matched")))
    return ""


def condition_entries(response: dict[str, Any]) -> list[dict[str, Any]]:
    first = first_result(response)
    entries: list[dict[str, Any]] = []
    for item in canonical_condition_results(response):
        entries.append(
            {
                "condition_id": str(item.get("condition_id") or item.get("条件ID") or ""),
                "condition": str(
                    item.get("condition")
                    or item.get("condition_text")
                    or item.get("条件")
                    or item.get("条件文本")
                    or ""
                ),
                "status": normalize_status(item.get("status") or item.get("判断状态")),
                "machine_status": str(item.get("status") or ""),
                "reason_code": str(item.get("reason_code") or ""),
                "reason": str(item.get("reason") or item.get("判定说明") or ""),
                "evidence": item.get("evidence") or item.get("evidence_items") or [],
                "source_decisions": item.get("source_decisions") or [],
                "raw": item,
            }
        )
    if entries:
        return entries

    per_condition = first.get("per_condition") or {}
    if isinstance(per_condition, dict):
        for text, item in per_condition.items():
            if not isinstance(item, dict):
                continue
            status = normalize_status(item.get("判断状态") or item.get("status"))
            if not status and "matched" in item and item.get("可判定", item.get("conclusive", True)):
                status = normalize_status(bool(item.get("matched")))
            entries.append(
                {
                    "condition_id": str(item.get("condition_id") or ""),
                    "condition": str(text),
                    "status": status,
                    "machine_status": str(item.get("status") or ""),
                    "reason_code": str(item.get("reason_code") or ""),
                    "reason": str(item.get("reason") or item.get("判定说明") or ""),
                    "evidence": item.get("evidence_items") or [],
                    "source_decisions": item.get("source_decisions") or [],
                    "audit": item.get("解释校验") if isinstance(item.get("解释校验"), dict) else {},
                    "raw": item,
                }
            )
    return entries


def _append_strings(target: list[str], value: Any) -> None:
    if isinstance(value, str) and value.strip():
        target.append(value.strip())
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            _append_strings(target, item)


def _dedupe(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        text = repair_mojibake(str(value or "")).strip()
        if text and text not in seen:
            seen.add(text)
            output.append(text)
    return output


def query_ir_conditions(response: dict[str, Any]) -> list[dict[str, Any]]:
    query_ir = response.get("查询IR") or response.get("query_ir") or {}
    if not isinstance(query_ir, dict):
        return []
    conditions = query_ir.get("子条件") or query_ir.get("conditions") or []
    return [item for item in conditions if isinstance(item, dict)] if isinstance(conditions, list) else []


def query_ir_root(response: dict[str, Any]) -> dict[str, Any]:
    query_ir = response.get("查询IR") or response.get("query_ir") or {}
    return query_ir if isinstance(query_ir, dict) else {}


def evidence_plan_conditions(response: dict[str, Any]) -> list[dict[str, Any]]:
    plan = response.get("证据计划") or response.get("evidence_plan") or {}
    if not isinstance(plan, dict):
        return []
    conditions = plan.get("conditions") or plan.get("子条件") or []
    return [item for item in conditions if isinstance(item, dict)] if isinstance(conditions, list) else []


def _condition_id(item: dict[str, Any]) -> str:
    return str(item.get("condition_id") or item.get("条件ID") or "")


def _condition_text(item: dict[str, Any]) -> str:
    return str(
        item.get("condition")
        or item.get("condition_text")
        or item.get("条件")
        or item.get("条件文本")
        or ""
    )


def _find_condition(
    items: Iterable[dict[str, Any]],
    selector: dict[str, Any],
) -> dict[str, Any] | None:
    condition_id = str(selector.get("condition_id") or "").strip()
    contains = str(selector.get("condition_contains") or "").strip()
    for item in items:
        if condition_id and _condition_id(item) != condition_id:
            continue
        if contains and contains not in _condition_text(item):
            continue
        return item
    return None


def _find_plan_source(
    response: dict[str, Any],
    selector: dict[str, Any],
) -> dict[str, Any] | None:
    condition = _find_condition(evidence_plan_conditions(response), selector)
    if condition is None:
        return None
    source_id = str(selector.get("source_id") or "").strip()
    source_contains = str(selector.get("source_contains") or "").strip()
    for source in condition.get("sources") or condition.get("证据源") or []:
        if not isinstance(source, dict):
            continue
        identity = str(
            source.get("source_id")
            or source.get("logical_source_id")
            or source.get("id")
            or ""
        )
        if source_id and identity != source_id:
            continue
        if source_contains and source_contains not in identity:
            continue
        return source
    return None


def _path_value(data: Any, path: str) -> Any:
    current = data
    for segment in str(path or "").split("."):
        if not segment:
            continue
        if isinstance(current, dict):
            current = current.get(segment)
        elif isinstance(current, list) and segment.isdigit():
            index = int(segment)
            current = current[index] if 0 <= index < len(current) else None
        else:
            return None
    return current


def condition_texts(response: dict[str, Any]) -> list[str]:
    texts: list[str] = []
    for item in query_ir_conditions(response):
        _append_strings(
            texts,
            item.get("条件文本") or item.get("condition_text") or item.get("condition"),
        )
    if texts:
        return _dedupe(texts)

    plan = response.get("证据计划") or response.get("evidence_plan") or {}
    if isinstance(plan, dict):
        conditions = plan.get("conditions") or plan.get("子条件") or []
        if isinstance(conditions, list):
            for item in conditions:
                if isinstance(item, dict):
                    _append_strings(
                        texts,
                        item.get("condition_text") or item.get("条件文本") or item.get("condition"),
                    )
    if texts:
        return _dedupe(texts)

    route = response.get("route") or {}
    if isinstance(route, dict):
        _append_strings(texts, route.get("sub_queries") or route.get("conditions"))
    if texts:
        return _dedupe(texts)

    _append_strings(texts, [entry["condition"] for entry in condition_entries(response)])
    return _dedupe(texts)


def planned_sources(response: dict[str, Any]) -> tuple[list[str], list[str]]:
    services: list[str] = []
    documents: list[str] = []

    for item in query_ir_conditions(response):
        _append_strings(services, item.get("目标服务") or item.get("target_skills") or item.get("target_services"))
        _append_strings(documents, item.get("目标文档") or item.get("target_docs") or item.get("target_documents"))

    plan = response.get("证据计划") or response.get("evidence_plan") or {}
    if isinstance(plan, dict):
        conditions = plan.get("conditions") or plan.get("子条件") or []
        if isinstance(conditions, list):
            for condition in conditions:
                if not isinstance(condition, dict):
                    continue
                sources = condition.get("sources") or condition.get("证据源") or []
                if not isinstance(sources, list):
                    continue
                for source in sources:
                    if not isinstance(source, dict):
                        continue
                    source_type = str(source.get("source_type") or source.get("type") or "").lower()
                    source_name = (
                        source.get("service_id")
                        or source.get("document")
                        or source.get("source_name")
                        or source.get("name")
                        or source.get("id")
                    )
                    if source_type in {"service", "skill", "structured"}:
                        _append_strings(services, source_name)
                    elif source_type in {"document", "doc", "record"}:
                        _append_strings(documents, source_name)

    route = response.get("route") or {}
    if isinstance(route, dict):
        _append_strings(services, route.get("services") or route.get("target_skills"))
        _append_strings(documents, route.get("documents") or route.get("target_docs"))

    return _dedupe(services), _dedupe(documents)


def planned_source_ids(response: dict[str, Any]) -> list[str]:
    source_ids: list[str] = []
    for item in query_ir_conditions(response):
        _append_strings(source_ids, item.get("evidence_plan_source_ids"))
        for service in item.get("目标服务") or item.get("target_services") or item.get("target_skills") or []:
            _append_strings(source_ids, (str(service), f"service:{service}"))
        for document in item.get("目标文档") or item.get("target_documents") or item.get("target_docs") or []:
            _append_strings(source_ids, (str(document), f"document:{document}"))

    plan = response.get("证据计划") or response.get("evidence_plan") or {}
    conditions = plan.get("conditions") or plan.get("子条件") or [] if isinstance(plan, dict) else []
    if isinstance(conditions, list):
        for condition in conditions:
            if not isinstance(condition, dict):
                continue
            for source in condition.get("sources") or condition.get("证据源") or []:
                if not isinstance(source, dict):
                    continue
                _append_strings(
                    source_ids,
                    source.get("source_id") or source.get("id") or source.get("logical_source_id"),
                )
    return _dedupe(source_ids)


def actual_evidence_source_ids(response: dict[str, Any]) -> list[str]:
    source_ids: list[str] = []
    for condition in canonical_condition_results(response):
        for decision in condition.get("source_decisions") or []:
            if isinstance(decision, dict):
                _append_strings(
                    source_ids,
                    decision.get("source_id") or decision.get("logical_source_id"),
                )
        for item in condition.get("evidence") or condition.get("evidence_items") or []:
            if not isinstance(item, dict):
                continue
            _append_strings(source_ids, item.get("source_id") or item.get("logical_source_id"))
            metadata = item.get("metadata") or {}
            if isinstance(metadata, dict):
                _append_strings(source_ids, metadata.get("logical_source_id") or metadata.get("source_id"))
    return _dedupe(source_ids)


def actual_evidence_sources(response: dict[str, Any]) -> list[str]:
    sources: list[str] = []
    first = first_result(response)

    structured = first.get("condition_results") or []
    if isinstance(structured, list):
        for condition in structured:
            if not isinstance(condition, dict):
                continue
            evidence = condition.get("evidence") or condition.get("evidence_items") or []
            if not isinstance(evidence, list):
                continue
            for item in evidence:
                if not isinstance(item, dict):
                    continue
                _append_strings(
                    sources,
                    item.get("source_name")
                    or item.get("document")
                    or item.get("service_id")
                    or item.get("file"),
                )
                _append_strings(sources, item.get("section"))

    per_condition = first.get("per_condition") or {}
    if isinstance(per_condition, dict):
        for condition in per_condition.values():
            if not isinstance(condition, dict):
                continue
            files = condition.get("files") or []
            if isinstance(files, list):
                for item in files:
                    if isinstance(item, dict):
                        _append_strings(sources, item.get("file") or item.get("source_name"))
                    else:
                        _append_strings(sources, item)
            _append_strings(sources, condition.get("docs") or condition.get("documents"))
            _append_strings(sources, condition.get("sections"))

    all_files = first.get("all_files") or []
    if isinstance(all_files, list):
        for item in all_files:
            if isinstance(item, dict):
                _append_strings(sources, item.get("file") or item.get("source_name"))
            else:
                _append_strings(sources, item)
    return _dedupe(sources)


def combined_reason(response: dict[str, Any]) -> str:
    first = first_result(response)
    reasons = [
        response.get("用户解释"),
        response.get("reason"),
        first.get("用户解释"),
        first.get("reason"),
    ]
    reasons.extend(entry.get("reason") for entry in condition_entries(response))
    return "\n".join(_dedupe(str(reason or "") for reason in reasons))


def is_data_blocked(status: str, reason: str) -> bool:
    return status == "无法判断" and any(marker in reason for marker in DATA_UNAVAILABLE_MARKERS)


def is_infrastructure_failure(response: dict[str, Any]) -> tuple[bool, str]:
    if response.get("__infrastructure_error__"):
        return True, str(response["__infrastructure_error__"])
    if response.get("error") and not response.get("results"):
        return True, str(response.get("error"))
    if response.get("detail") and not response.get("results"):
        return True, str(response.get("detail"))
    return False, ""


def _contains(actual_values: Iterable[str], expected: str) -> bool:
    expected_text = repair_mojibake(str(expected)).strip().lower()
    return any(expected_text in repair_mojibake(str(value)).lower() for value in actual_values)


def _has_source_identity(actual_values: Iterable[str], expected: str) -> bool:
    expected_text = repair_mojibake(str(expected)).strip().lower()
    if not expected_text:
        return False
    for value in actual_values:
        actual = repair_mojibake(str(value)).strip().lower()
        if actual == expected_text or actual.endswith(f":{expected_text}"):
            return True
    return False


def _assertion(
    name: str,
    group: str,
    passed: bool,
    expected: Any,
    actual: Any,
    *,
    layer: str,
    code: str,
    blocked: bool = False,
    detail: str = "",
) -> dict[str, Any]:
    return {
        "name": name,
        "group": group,
        "layer": layer,
        "code": code,
        "outcome": "BLOCKED" if blocked else ("PASS" if passed else "FAIL"),
        "expected": expected,
        "actual": actual,
        "detail": detail,
    }


def _append_field_assertions(
    assertions: list[dict[str, Any]],
    *,
    name_prefix: str,
    group: str,
    layer: str,
    code_prefix: str,
    actual: dict[str, Any] | None,
    expected_fields: dict[str, Any],
) -> None:
    for path, expected_value in expected_fields.items():
        actual_value = _path_value(actual or {}, str(path))
        assertions.append(
            _assertion(
                f"{name_prefix}:{path}",
                group,
                actual_value == expected_value,
                expected_value,
                actual_value,
                layer=layer,
                code=f"{code_prefix}_FIELD_MISMATCH",
                detail=f"path={path}",
            )
        )


def _layer_results(assertions: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    for layer in LAYER_ORDER:
        items = [item for item in assertions if item.get("layer") == layer]
        outcomes = {item.get("outcome") for item in items}
        if not items:
            status = "NOT_EVALUATED"
        elif "FAIL" in outcomes:
            status = "FAIL"
        elif "BLOCKED" in outcomes:
            status = "BLOCKED"
        else:
            status = "PASS"
        results[layer] = {
            "label": LAYER_LABELS[layer],
            "status": status,
            "assertion_count": len(items),
            "failure_codes": _dedupe(
                item.get("code") for item in items if item.get("outcome") == "FAIL"
            ),
            "blocked_codes": _dedupe(
                item.get("code") for item in items if item.get("outcome") == "BLOCKED"
            ),
        }
    return results


def _first_layer_with_status(layers: dict[str, dict[str, Any]], status: str) -> str | None:
    return next(
        (layer for layer in LAYER_ORDER if layers.get(layer, {}).get("status") == status),
        None,
    )


def _condition_evidence(item: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(item, dict):
        return []
    evidence = item.get("evidence") or item.get("evidence_items") or []
    return [entry for entry in evidence if isinstance(entry, dict)] if isinstance(evidence, list) else []


def _evidence_source_ids(items: Iterable[dict[str, Any]]) -> list[str]:
    source_ids: list[str] = []
    for item in items:
        _append_strings(source_ids, item.get("source_id") or item.get("logical_source_id"))
        metadata = item.get("metadata") or {}
        if isinstance(metadata, dict):
            _append_strings(source_ids, metadata.get("logical_source_id") or metadata.get("source_id"))
    return _dedupe(source_ids)


def _evidence_record_ids(items: Iterable[dict[str, Any]]) -> list[str]:
    values: list[str] = []
    for item in items:
        _append_strings(values, item.get("record_id") or item.get("record_ids"))
    return _dedupe(values)


def _explanation_audit_for(
    response: dict[str, Any],
    assertion: dict[str, Any],
) -> dict[str, Any] | None:
    first = first_result(response)
    scope = str(assertion.get("scope") or "overall").strip().lower()
    if scope == "overall":
        audit = first.get("解释校验") or response.get("解释校验")
        return audit if isinstance(audit, dict) else None

    per_condition = first.get("per_condition") or {}
    if not isinstance(per_condition, dict):
        return None
    selector = {
        "condition_id": assertion.get("condition_id"),
        "condition_contains": assertion.get("condition_contains"),
    }
    for text, info in per_condition.items():
        if not isinstance(info, dict):
            continue
        condition_result = info.get("condition_result") or {}
        candidate = dict(condition_result) if isinstance(condition_result, dict) else {}
        candidate.setdefault("condition", str(text))
        if _find_condition([candidate], selector) is None:
            continue
        if scope == "condition":
            audit = info.get("解释校验")
            return audit if isinstance(audit, dict) else None
        if scope == "source":
            source_id = str(assertion.get("source_id") or "").strip()
            source_contains = str(assertion.get("source_contains") or "").strip()
            for source in info.get("files") or []:
                if not isinstance(source, dict):
                    continue
                identity = str(
                    source.get("logical_source_id")
                    or source.get("source_id")
                    or source.get("file")
                    or source.get("source_name")
                    or ""
                )
                if source_id and identity != source_id:
                    continue
                if source_contains and source_contains not in identity:
                    continue
                audit = source.get("解释校验")
                return audit if isinstance(audit, dict) else None
    return None


def response_review_projection(response: dict[str, Any]) -> dict[str, Any]:
    """Return the stable semantic subset used to bind a human review."""
    response = canonicalize(response)
    first = first_result(response)
    conditions = canonical_condition_results(response)
    projection = {
        "query_normalization": response.get("查询归一化")
        or response.get("query_normalization")
        or {},
        "query_ir": response.get("查询IR") or response.get("query_ir") or {},
        "evidence_plan": response.get("证据计划") or response.get("evidence_plan") or {},
        "route": response.get("route") or {},
        "condition_results": conditions,
        "overall_result": canonical_overall_result(response),
    }
    if not conditions:
        projection["legacy_condition_results"] = first.get("per_condition") or {}
        projection["legacy_overall_status"] = overall_status(response)
    return projection


def response_review_fingerprint(response: dict[str, Any]) -> str:
    payload = json.dumps(
        response_review_projection(response),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _case_review(case: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
    configured = case.get("review") if isinstance(case.get("review"), dict) else {}
    status = str(configured.get("status") or "pending").strip().lower()
    expected_hash = str(
        configured.get("source_response_sha256")
        or configured.get("response_sha256")
        or ""
    ).strip().lower()
    actual_hash = response_review_fingerprint(response)
    if expected_hash:
        binding_status = "current" if expected_hash == actual_hash else "stale"
    else:
        binding_status = "unbound"
    clinical_fields = _clinical_expectations(case.get("expected") or {})
    provenance_complete = bool(
        str(configured.get("reviewed_by") or "").strip()
        and _valid_reviewed_at(configured.get("reviewed_at"))
        and str(configured.get("note") or "").strip()
    )
    return {
        "status": status,
        "reviewed_by": str(configured.get("reviewed_by") or ""),
        "reviewed_at": str(configured.get("reviewed_at") or ""),
        "note": str(configured.get("note") or ""),
        "clinical_expectations": clinical_fields,
        "provenance_complete": provenance_complete,
        "binding_status": binding_status,
        "expected_response_sha256": expected_hash,
        "actual_response_sha256": actual_hash,
        "clinical_accuracy_eligible": bool(
            clinical_fields
            and status == "verified"
            and provenance_complete
            and binding_status == "current"
        ),
    }


def _apply_review_policy(
    assertions: list[dict[str, Any]], review: dict[str, Any]
) -> None:
    if not review.get("clinical_expectations"):
        return
    code = ""
    detail = ""
    if review.get("status") != "verified":
        code = "CLINICAL_REVIEW_REQUIRED"
        detail = "Clinical outcome assertions require a verified human review."
    elif review.get("binding_status") == "stale":
        code = "REVIEW_RESPONSE_DRIFT"
        detail = "The current semantic response fingerprint differs from the reviewed response."
    if not code:
        return
    for assertion in assertions:
        if assertion.get("group") != "clinical":
            continue
        assertion["original_outcome"] = assertion.get("outcome")
        assertion["original_code"] = assertion.get("code")
        assertion["outcome"] = "BLOCKED"
        assertion["code"] = code
        assertion["detail"] = detail


def _request_trace_summary(response: dict[str, Any]) -> dict[str, Any]:
    trace = response.get("request_trace")
    if not isinstance(trace, dict):
        trace = first_result(response).get("request_trace")
    trace_origin = "native"
    if not isinstance(trace, dict):
        timings = response.get("timings")
        if not isinstance(timings, dict) or not timings:
            return {}
        from microharness.medical.request_trace import build_medical_query_trace

        trace = build_medical_query_trace(
            response,
            request_id=str(response.get("request_id") or ""),
            lifecycle_status="completed",
        )
        trace_origin = "legacy_synthesized"
    issues = [item for item in trace.get("issues") or [] if isinstance(item, dict)]
    return {
        "origin": trace_origin,
        "schema_version": str(trace.get("schema_version") or ""),
        "lifecycle_status": str(trace.get("lifecycle_status") or ""),
        "outcome": trace.get("outcome") if isinstance(trace.get("outcome"), dict) else {},
        "models": trace.get("models") if isinstance(trace.get("models"), dict) else {},
        "queue": trace.get("queue") if isinstance(trace.get("queue"), dict) else {},
        "timings": trace.get("timings") if isinstance(trace.get("timings"), dict) else {},
        "bottleneck": trace.get("bottleneck")
        if isinstance(trace.get("bottleneck"), dict)
        else {},
        "sources": trace.get("sources") if isinstance(trace.get("sources"), dict) else {},
        "explanations": trace.get("explanations")
        if isinstance(trace.get("explanations"), dict)
        else {},
        "issues": issues,
        "first_issue": trace.get("first_issue")
        if isinstance(trace.get("first_issue"), dict)
        else {},
    }


def evaluate_case(case: dict[str, Any], response: dict[str, Any], latency_ms: float | None) -> dict[str, Any]:
    response = canonicalize(response)
    failed, failure_reason = is_infrastructure_failure(response)
    if failed:
        layers = _layer_results([])
        configured_review = case.get("review") if isinstance(case.get("review"), dict) else {}
        return {
            "case_id": case["id"],
            "title": case.get("title", ""),
            "categories": list(case.get("category") or []),
            "status": "infrastructure_failure",
            "latency_ms": latency_ms,
            "infrastructure_error": failure_reason,
            "review": {
                "status": str(configured_review.get("status") or "pending").lower(),
                "binding_status": "unavailable",
                "clinical_expectations": _clinical_expectations(case.get("expected") or {}),
                "clinical_accuracy_eligible": False,
            },
            "request_trace": _request_trace_summary(response),
            "assertions": [],
            "layers": layers,
            "first_failure_layer": None,
            "first_blocked_layer": None,
            "failure_codes": ["INFRASTRUCTURE_FAILURE"],
        }

    expected = case.get("expected") or {}
    assertions: list[dict[str, Any]] = []
    status = overall_status(response)
    reason = combined_reason(response)
    entries = condition_entries(response)
    texts = condition_texts(response)
    services, documents = planned_sources(response)
    source_ids = planned_source_ids(response)
    evidence_sources = actual_evidence_sources(response)
    evidence_source_ids = actual_evidence_source_ids(response)
    ir_root = query_ir_root(response)
    ir_conditions = query_ir_conditions(response)
    canonical_conditions = canonical_condition_results(response)
    canonical_overall = canonical_overall_result(response)
    review = _case_review(case, response)
    trace = _request_trace_summary(response)

    if "overall_status" in expected:
        expected_status = normalize_status(expected["overall_status"])
        assertions.append(
            _assertion(
                "overall_status",
                "clinical",
                status == expected_status,
                expected_status,
                status,
                layer="overall_adjudication",
                code="OVERALL_STATUS_MISMATCH",
                blocked=status != expected_status and is_data_blocked(status, reason),
                detail=reason,
            )
        )

    if "condition_count" in expected:
        assertions.append(
            _assertion(
                "condition_count",
                "routing",
                len(texts) == int(expected["condition_count"]),
                int(expected["condition_count"]),
                len(texts),
                layer="understanding",
                code="UNDERSTANDING_CONDITION_COUNT_MISMATCH",
                detail=" | ".join(texts),
            )
        )

    for required in expected.get("required_condition_contains") or []:
        assertions.append(
            _assertion(
                f"condition_contains:{required}",
                "routing",
                _contains(texts, required),
                required,
                texts,
                layer="understanding",
                code="UNDERSTANDING_CONDITION_MISSING",
            )
        )

    if "query_type" in expected:
        actual_type = str(ir_root.get("类型") or ir_root.get("query_type") or "").lower()
        expected_type = str(expected.get("query_type") or "").lower()
        assertions.append(
            _assertion(
                "query_type",
                "routing",
                actual_type == expected_type,
                expected_type,
                actual_type,
                layer="understanding",
                code="UNDERSTANDING_QUERY_TYPE_MISMATCH",
            )
        )

    normalization_expected = expected.get("normalization") or {}
    if isinstance(normalization_expected, dict) and normalization_expected:
        normalization = response.get("查询归一化") or response.get("query_normalization") or {}
        _append_field_assertions(
            assertions,
            name_prefix="normalization",
            group="routing",
            layer="understanding",
            code_prefix="UNDERSTANDING_NORMALIZATION",
            actual=normalization if isinstance(normalization, dict) else {},
            expected_fields=normalization_expected,
        )

    if "connector" in expected:
        actual_connector = str(ir_root.get("连接关系") or ir_root.get("connector") or "").lower()
        expected_connector = str(expected.get("connector") or "").lower()
        assertions.append(
            _assertion(
                "connector",
                "routing",
                actual_connector == expected_connector,
                expected_connector,
                actual_connector,
                layer="understanding",
                code="UNDERSTANDING_CONNECTOR_MISMATCH",
            )
        )
    for index, ir_expected in enumerate(expected.get("condition_ir") or [], 1):
        if not isinstance(ir_expected, dict):
            continue
        actual_ir = _find_condition(ir_conditions, ir_expected)
        selector = ir_expected.get("condition_id") or ir_expected.get("condition_contains") or index
        assertions.append(
            _assertion(
                f"ir_condition:{selector}",
                "routing",
                actual_ir is not None,
                selector,
                _condition_text(actual_ir) if actual_ir else None,
                layer="ir",
                code="IR_CONDITION_NOT_FOUND",
            )
        )
        if actual_ir is not None:
            fields = ir_expected.get("fields") or ir_expected.get("required_fields") or {}
            if isinstance(fields, dict):
                _append_field_assertions(
                    assertions,
                    name_prefix=f"ir_field:{selector}",
                    group="routing",
                    layer="ir",
                    code_prefix="IR",
                    actual=actual_ir,
                    expected_fields=fields,
                )

    for condition_expected in expected.get("condition_statuses") or []:
        needle = str(condition_expected.get("condition_contains") or "")
        matching = _find_condition(entries, condition_expected)
        actual_status = matching["status"] if matching else ""
        actual_reason = matching["reason"] if matching else ""
        expected_status = normalize_status(condition_expected.get("status"))
        assertions.append(
            _assertion(
                f"condition_status:{needle or condition_expected.get('condition_id') or ''}",
                "clinical",
                actual_status == expected_status,
                expected_status,
                actual_status,
                layer="condition_adjudication",
                code="CONDITION_STATUS_MISMATCH",
                blocked=actual_status != expected_status and is_data_blocked(actual_status, actual_reason),
                detail=actual_reason,
            )
        )
        canonical = _find_condition(canonical_conditions, condition_expected)
        for field_name in ("reason_code", "data_quality", "conflict_level"):
            if field_name not in condition_expected:
                continue
            actual_value = canonical.get(field_name) if canonical else None
            assertions.append(
                _assertion(
                    f"condition_{field_name}:{needle or condition_expected.get('condition_id') or ''}",
                    "clinical",
                    actual_value == condition_expected[field_name],
                    condition_expected[field_name],
                    actual_value,
                    layer="condition_adjudication",
                    code=f"CONDITION_{field_name.upper()}_MISMATCH",
                    detail=actual_reason,
                )
            )

    for service in expected.get("required_services") or []:
        assertions.append(
            _assertion(
                f"required_service:{service}",
                "routing",
                _contains(services, service),
                service,
                services,
                layer="routing",
                code="ROUTING_REQUIRED_SERVICE_MISSING",
            )
        )

    for document in expected.get("required_documents") or []:
        assertions.append(
            _assertion(
                f"required_document:{document}",
                "routing",
                _contains(documents, document),
                document,
                documents,
                layer="routing",
                code="ROUTING_REQUIRED_DOCUMENT_MISSING",
            )
        )

    for source_id in expected.get("required_source_ids") or []:
        assertions.append(
            _assertion(
                f"required_source_id:{source_id}",
                "routing",
                _has_source_identity(source_ids, source_id),
                source_id,
                source_ids,
                layer="routing",
                code="ROUTING_REQUIRED_SOURCE_ID_MISSING",
            )
        )

    for index, routing_expected in enumerate(expected.get("routing_assertions") or [], 1):
        if not isinstance(routing_expected, dict):
            continue
        source = _find_plan_source(response, routing_expected)
        selector = routing_expected.get("source_id") or routing_expected.get("source_contains") or index
        assertions.append(
            _assertion(
                f"routing_source:{selector}",
                "routing",
                source is not None,
                selector,
                source,
                layer="routing",
                code="ROUTING_SOURCE_PLAN_MISSING",
            )
        )
        fields = routing_expected.get("fields") or {}
        if source is not None and isinstance(fields, dict):
            _append_field_assertions(
                assertions,
                name_prefix=f"routing_source_field:{selector}",
                group="routing",
                layer="routing",
                code_prefix="ROUTING_SOURCE",
                actual=source,
                expected_fields=fields,
            )

    for source in expected.get("required_evidence_sources") or []:
        assertions.append(
            _assertion(
                f"required_evidence:{source}",
                "evidence",
                _contains(evidence_sources, source),
                source,
                evidence_sources,
                layer="evidence",
                code="EVIDENCE_REQUIRED_SOURCE_MISSING",
            )
        )

    for source_id in expected.get("required_evidence_source_ids") or []:
        assertions.append(
            _assertion(
                f"required_evidence_source_id:{source_id}",
                "evidence",
                _has_source_identity(evidence_source_ids, source_id),
                source_id,
                evidence_source_ids,
                layer="evidence",
                code="EVIDENCE_REQUIRED_SOURCE_ID_MISSING",
            )
        )

    for index, evidence_expected in enumerate(expected.get("evidence_assertions") or [], 1):
        if not isinstance(evidence_expected, dict):
            continue
        condition = _find_condition(canonical_conditions, evidence_expected)
        selector = evidence_expected.get("condition_id") or evidence_expected.get("condition_contains") or index
        assertions.append(
            _assertion(
                f"evidence_condition:{selector}",
                "evidence",
                condition is not None,
                selector,
                _condition_text(condition) if condition else None,
                layer="evidence",
                code="EVIDENCE_CONDITION_RESULT_MISSING",
            )
        )
        if condition is None:
            continue
        items = _condition_evidence(condition)
        if "min_count" in evidence_expected:
            minimum = int(evidence_expected.get("min_count") or 0)
            assertions.append(
                _assertion(
                    f"evidence_min_count:{selector}",
                    "evidence",
                    len(items) >= minimum,
                    minimum,
                    len(items),
                    layer="evidence",
                    code="EVIDENCE_COUNT_BELOW_MINIMUM",
                )
            )
        item_source_ids = _evidence_source_ids(items)
        decision_source_ids = _dedupe(
            str(decision.get("source_id") or decision.get("logical_source_id") or "")
            for decision in condition.get("source_decisions") or []
            if isinstance(decision, dict)
        )
        all_condition_source_ids = _dedupe([*item_source_ids, *decision_source_ids])
        for source_id in evidence_expected.get("required_source_ids") or []:
            assertions.append(
                _assertion(
                    f"evidence_source_id:{selector}:{source_id}",
                    "evidence",
                    _has_source_identity(all_condition_source_ids, source_id),
                    source_id,
                    all_condition_source_ids,
                    layer="evidence",
                    code="EVIDENCE_REQUIRED_SOURCE_ID_MISSING",
                )
            )
        record_ids = _evidence_record_ids(items)
        for record_id in evidence_expected.get("required_record_ids") or []:
            assertions.append(
                _assertion(
                    f"evidence_record_id:{selector}:{record_id}",
                    "evidence",
                    record_id in record_ids,
                    record_id,
                    record_ids,
                    layer="evidence",
                    code="EVIDENCE_REQUIRED_RECORD_ID_MISSING",
                )
            )
        fields_any = evidence_expected.get("fields_any") or {}
        if isinstance(fields_any, dict):
            for path, expected_value in fields_any.items():
                actual_values = [_path_value(item, str(path)) for item in items]
                assertions.append(
                    _assertion(
                        f"evidence_field_any:{selector}:{path}",
                        "evidence",
                        expected_value in actual_values,
                        expected_value,
                        actual_values,
                        layer="evidence",
                        code="EVIDENCE_FACT_NOT_FOUND",
                        detail=f"path={path}",
                    )
                )

    for index, temporal_expected in enumerate(expected.get("temporal_assertions") or [], 1):
        if not isinstance(temporal_expected, dict):
            continue
        selector = temporal_expected.get("condition_id") or temporal_expected.get("condition_contains") or index
        actual_ir = _find_condition(ir_conditions, temporal_expected)
        temporal = None
        if actual_ir is not None:
            temporal = actual_ir.get("时间约束") or actual_ir.get("temporal")
        assertions.append(
            _assertion(
                f"temporal_condition:{selector}",
                "routing",
                isinstance(temporal, dict),
                "temporal IR",
                temporal,
                layer="temporal",
                code="TEMPORAL_IR_MISSING",
            )
        )
        fields = temporal_expected.get("fields") or {}
        if isinstance(temporal, dict) and isinstance(fields, dict):
            _append_field_assertions(
                assertions,
                name_prefix=f"temporal_field:{selector}",
                group="routing",
                layer="temporal",
                code_prefix="TEMPORAL",
                actual=temporal,
                expected_fields=fields,
            )
        evidence_fields_any = temporal_expected.get("evidence_fields_any") or {}
        if isinstance(evidence_fields_any, dict):
            condition = _find_condition(canonical_conditions, temporal_expected)
            items = _condition_evidence(condition)
            for path, expected_value in evidence_fields_any.items():
                actual_values = [
                    _path_value(item.get("metadata") or {}, str(path))
                    for item in items
                ]
                blocked = not items and bool(condition) and normalize_status(condition.get("status")) == "无法判断"
                assertions.append(
                    _assertion(
                        f"temporal_evidence_any:{selector}:{path}",
                        "evidence",
                        expected_value in actual_values,
                        expected_value,
                        actual_values,
                        layer="temporal",
                        code="TEMPORAL_EVIDENCE_FACT_NOT_FOUND",
                        blocked=blocked,
                        detail=f"metadata path={path}",
                    )
                )

    overall_expected = expected.get("overall_result") or {}
    if isinstance(overall_expected, dict) and overall_expected:
        _append_field_assertions(
            assertions,
            name_prefix="overall_result",
            group="clinical",
            layer="overall_adjudication",
            code_prefix="OVERALL_ADJUDICATION",
            actual=canonical_overall,
            expected_fields=overall_expected,
        )

    for required in expected.get("required_reason_contains") or []:
        assertions.append(
            _assertion(
                f"reason_contains:{required}",
                "explanation",
                required in reason,
                required,
                reason,
                layer="explanation",
                code="EXPLANATION_REQUIRED_TEXT_MISSING",
            )
        )

    for forbidden in expected.get("forbidden_reason_contains") or []:
        assertions.append(
            _assertion(
                f"reason_not_contains:{forbidden}",
                "explanation",
                forbidden not in reason,
                f"not contains {forbidden}",
                reason,
                layer="explanation",
                code="EXPLANATION_FORBIDDEN_TEXT_PRESENT",
            )
        )

    for index, audit_expected in enumerate(expected.get("explanation_audits") or [], 1):
        if not isinstance(audit_expected, dict):
            continue
        audit = _explanation_audit_for(response, audit_expected)
        scope = str(audit_expected.get("scope") or "overall")
        selector = audit_expected.get("condition_id") or audit_expected.get("condition_contains") or index
        assertions.append(
            _assertion(
                f"explanation_audit:{scope}:{selector}",
                "explanation",
                audit is not None,
                "audit present",
                audit,
                layer="explanation",
                code="EXPLANATION_AUDIT_MISSING",
            )
        )
        fields = audit_expected.get("fields") or {}
        if audit is not None and isinstance(fields, dict):
            _append_field_assertions(
                assertions,
                name_prefix=f"explanation_audit_field:{scope}:{selector}",
                group="explanation",
                layer="explanation",
                code_prefix="EXPLANATION_AUDIT",
                actual=audit,
                expected_fields=fields,
            )

    _apply_review_policy(assertions, review)
    outcomes = {item["outcome"] for item in assertions}
    case_status = "FAIL" if "FAIL" in outcomes else ("BLOCKED" if "BLOCKED" in outcomes else "PASS")
    layers = _layer_results(assertions)
    domains = _dedupe(
        str(item.get("领域") or item.get("domain") or "")
        for item in ir_conditions
        if str(item.get("领域") or item.get("domain") or "").strip()
    )
    temporal_relations: list[str] = []
    for item in ir_conditions:
        temporal = item.get("时间约束") or item.get("temporal_constraint") or {}
        if isinstance(temporal, dict):
            _append_strings(temporal_relations, temporal.get("关系") or temporal.get("relation"))
    source_trace = trace.get("sources") if isinstance(trace.get("sources"), dict) else {}
    unknown_uncertainty_counts = (
        source_trace.get("unknown_uncertainty_kind_counts")
        if isinstance(source_trace.get("unknown_uncertainty_kind_counts"), dict)
        else {}
    )
    source_uncertainty = _dedupe(
        str(kind)
        for kind, count in unknown_uncertainty_counts.items()
        if isinstance(count, (int, float)) and count > 0
    )
    source_health = "unknown"
    if trace:
        if int(source_trace.get("unavailable_count") or 0) > 0:
            source_health = "unavailable"
        elif int(source_trace.get("degraded_count") or 0) > 0:
            source_health = "degraded"
        else:
            source_health = "complete"
    return {
        "case_id": case["id"],
        "title": case.get("title", ""),
        "categories": list(case.get("category") or []),
        "status": case_status,
        "latency_ms": latency_ms,
        "review": review,
        "request_trace": trace,
        "dimensions": {
            "domains": domains,
            "temporal_relations": _dedupe(temporal_relations),
            "review_status": review.get("status"),
            "review_binding": review.get("binding_status"),
            "overall_status": status,
            "source_health": source_health,
            "source_uncertainty": source_uncertainty or ["none"],
        },
        "observed": {
            "overall_status": status,
            "condition_texts": texts,
            "planned_services": services,
            "planned_documents": documents,
            "planned_source_ids": source_ids,
            "evidence_sources": evidence_sources,
            "evidence_source_ids": evidence_source_ids,
            "canonical_overall_result": canonical_overall,
        },
        "assertions": assertions,
        "layers": layers,
        "first_failure_layer": _first_layer_with_status(layers, "FAIL"),
        "first_blocked_layer": _first_layer_with_status(layers, "BLOCKED"),
        "failure_codes": _dedupe(
            item.get("code") for item in assertions if item.get("outcome") == "FAIL"
        ),
        "blocked_codes": _dedupe(
            item.get("code") for item in assertions if item.get("outcome") == "BLOCKED"
        ),
    }


def post_json(endpoint: str, payload: dict[str, Any], timeout: float) -> tuple[dict[str, Any], float]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = request.Request(endpoint, data=body, headers={"Content-Type": "application/json"}, method="POST")
    started = time.perf_counter()
    try:
        with request.urlopen(req, timeout=timeout) as response:
            raw = response.read()
            elapsed_ms = (time.perf_counter() - started) * 1000
            return canonicalize(json.loads(raw.decode("utf-8-sig"))), elapsed_ms
    except error.HTTPError as exc:
        elapsed_ms = (time.perf_counter() - started) * 1000
        raw = exc.read()
        detail = raw.decode("utf-8", errors="replace") if raw else str(exc)
        return {"__infrastructure_error__": f"HTTP {exc.code}: {detail}"}, elapsed_ms
    except (error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        elapsed_ms = (time.perf_counter() - started) * 1000
        return {"__infrastructure_error__": f"{type(exc).__name__}: {exc}"}, elapsed_ms


def replay_response(path: Path, case_id: str) -> tuple[dict[str, Any], float | None]:
    source = path / f"{case_id}.json" if path.is_dir() else path
    if not source.exists():
        return {"__infrastructure_error__": f"回放响应不存在: {source}"}, None
    data = load_json(source)
    if not isinstance(data, dict):
        return {"__infrastructure_error__": f"回放响应不是 JSON 对象: {source}"}, None
    if isinstance(data.get("responses"), dict):
        data = data["responses"].get(case_id) or {}
    elif case_id in data and isinstance(data[case_id], dict):
        data = data[case_id]
    latency = data.get("total_ms") if isinstance(data, dict) else None
    return data, float(latency) if isinstance(latency, (int, float)) else None


def percentile(values: list[float], ratio: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * ratio
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return round(ordered[lower], 2)
    weight = position - lower
    return round(ordered[lower] * (1 - weight) + ordered[upper] * weight, 2)


def assertion_metric(assertions: list[dict[str, Any]]) -> dict[str, Any]:
    passed = sum(item["outcome"] == "PASS" for item in assertions)
    failed = sum(item["outcome"] == "FAIL" for item in assertions)
    blocked = sum(item["outcome"] == "BLOCKED" for item in assertions)
    scored = passed + failed
    return {
        "passed": passed,
        "failed": failed,
        "blocked": blocked,
        "total": len(assertions),
        "pass_rate_excluding_blocked": round(passed / scored, 4) if scored else None,
    }


def _case_metric(results: list[dict[str, Any]]) -> dict[str, Any]:
    counts = Counter(str(item.get("status") or "unknown") for item in results)
    scored = counts["PASS"] + counts["FAIL"]
    return {
        "total": len(results),
        "status_counts": dict(sorted(counts.items())),
        "pass_rate_excluding_blocked": round(counts["PASS"] / scored, 4)
        if scored
        else None,
    }


def _numeric_metric(values: Iterable[Any]) -> dict[str, Any]:
    numbers = [float(value) for value in values if isinstance(value, (int, float))]
    return {
        "count": len(numbers),
        "mean": round(statistics.fmean(numbers), 2) if numbers else None,
        "p50": percentile(numbers, 0.50),
        "p95": percentile(numbers, 0.95),
    }


def _segment_values(result: dict[str, Any]) -> dict[str, list[str]]:
    dimensions = result.get("dimensions") if isinstance(result.get("dimensions"), dict) else {}
    trace = result.get("request_trace") if isinstance(result.get("request_trace"), dict) else {}
    models = trace.get("models") if isinstance(trace.get("models"), dict) else {}
    values = {
        "category": [str(value) for value in result.get("categories") or []],
        "domain": [str(value) for value in dimensions.get("domains") or []],
        "temporal_relation": [
            str(value) for value in dimensions.get("temporal_relations") or []
        ],
        "review_status": [str(dimensions.get("review_status") or "unknown")],
        "review_binding": [str(dimensions.get("review_binding") or "unknown")],
        "overall_status": [str(dimensions.get("overall_status") or "unknown")],
        "source_health": [str(dimensions.get("source_health") or "unknown")],
        "source_uncertainty": [
            str(value) for value in dimensions.get("source_uncertainty") or ["none"]
        ],
        "first_failure_layer": [str(result.get("first_failure_layer") or "none")],
        "model": [
            f"{role}={model}"
            for role, model in sorted(models.items())
            if str(model).strip()
        ],
    }
    return {
        name: _dedupe(value for value in candidates if str(value).strip())
        for name, candidates in values.items()
    }


def _segment_metrics(results: list[dict[str, Any]]) -> dict[str, Any]:
    buckets: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for result in results:
        for dimension, values in _segment_values(result).items():
            dimension_buckets = buckets.setdefault(dimension, {})
            for value in values:
                dimension_buckets.setdefault(value, []).append(result)
    segmented: dict[str, Any] = {}
    for dimension, dimension_buckets in sorted(buckets.items()):
        segmented[dimension] = {}
        for value, items in sorted(dimension_buckets.items()):
            metric = _case_metric(items)
            assertions = [
                assertion for item in items for assertion in item.get("assertions", [])
            ]
            metric["assertion_groups"] = {
                group: assertion_metric(
                    [entry for entry in assertions if entry.get("group") == group]
                )
                for group in ASSERTION_GROUPS
            }
            metric["layers"] = {
                layer: assertion_metric(
                    [entry for entry in assertions if entry.get("layer") == layer]
                )
                for layer in LAYER_ORDER
            }
            segmented[dimension][value] = metric
    return segmented


def _review_metrics(results: list[dict[str, Any]]) -> dict[str, Any]:
    reviews = [item.get("review") or {} for item in results]
    status_counts = Counter(str(review.get("status") or "unknown") for review in reviews)
    binding_counts = Counter(
        str(review.get("binding_status") or "unknown") for review in reviews
    )
    clinical_results = [
        result
        for result in results
        if (result.get("review") or {}).get("clinical_expectations")
    ]
    eligible_results = [
        result
        for result in clinical_results
        if (result.get("review") or {}).get("clinical_accuracy_eligible") is True
    ]
    eligible_ids = {result.get("case_id") for result in eligible_results}
    eligible_assertions = [
        assertion
        for result in results
        if result.get("case_id") in eligible_ids
        for assertion in result.get("assertions", [])
        if assertion.get("group") == "clinical"
    ]
    return {
        "status_counts": dict(sorted(status_counts.items())),
        "binding_counts": dict(sorted(binding_counts.items())),
        "provenance_complete": sum(
            review.get("provenance_complete") is True for review in reviews
        ),
        "clinical_cases": len(clinical_results),
        "bound_clinical_cases": len(eligible_results),
        "unbound_or_stale_clinical_cases": len(clinical_results) - len(eligible_results),
        "stale_case_ids": [
            result.get("case_id")
            for result in clinical_results
            if (result.get("review") or {}).get("binding_status") == "stale"
        ],
        "unbound_case_ids": [
            result.get("case_id")
            for result in clinical_results
            if (result.get("review") or {}).get("binding_status") == "unbound"
        ],
        "bound_clinical_accuracy": assertion_metric(eligible_assertions),
    }


def _trace_metrics(results: list[dict[str, Any]]) -> dict[str, Any]:
    traces = [
        result.get("request_trace")
        for result in results
        if isinstance(result.get("request_trace"), dict) and result.get("request_trace")
    ]
    origin_counts = Counter(str(trace.get("origin") or "unknown") for trace in traces)
    lifecycle_counts = Counter(str(trace.get("lifecycle_status") or "unknown") for trace in traces)
    bottleneck_counts = Counter(
        str((trace.get("bottleneck") or {}).get("stage") or "unknown") for trace in traces
    )
    first_issue_layer_counts = Counter(
        str((trace.get("first_issue") or {}).get("layer") or "none") for trace in traces
    )
    first_issue_code_counts = Counter(
        str((trace.get("first_issue") or {}).get("code") or "none") for trace in traces
    )
    issue_code_counts: Counter[str] = Counter()
    source_uncertainty_kind_counts: Counter[str] = Counter()
    source_uncertainty_case_counts: Counter[str] = Counter()
    model_counts: dict[str, Counter[str]] = {}
    stage_values: dict[str, list[float]] = {}
    for trace in traces:
        source_trace = trace.get("sources") if isinstance(trace.get("sources"), dict) else {}
        uncertainty_counts = (
            source_trace.get("unknown_uncertainty_kind_counts")
            if isinstance(source_trace.get("unknown_uncertainty_kind_counts"), dict)
            else {}
        )
        for kind, count in uncertainty_counts.items():
            if not isinstance(count, (int, float)) or count <= 0:
                continue
            source_uncertainty_kind_counts[str(kind)] += int(count)
            source_uncertainty_case_counts[str(kind)] += 1
        for issue in trace.get("issues") or []:
            if not isinstance(issue, dict):
                continue
            code = str(issue.get("code") or "unknown")
            count = issue.get("count")
            issue_code_counts[code] += int(count) if isinstance(count, (int, float)) else 1
        for role, model in (trace.get("models") or {}).items():
            model_counts.setdefault(str(role), Counter())[str(model)] += 1
        for stage, elapsed in (trace.get("timings") or {}).items():
            if isinstance(elapsed, (int, float)):
                stage_values.setdefault(str(stage), []).append(float(elapsed))
    source_unavailable_cases = sum(
        int((trace.get("sources") or {}).get("unavailable_count") or 0) > 0
        for trace in traces
    )
    source_degraded_cases = sum(
        int((trace.get("sources") or {}).get("degraded_count") or 0) > 0
        for trace in traces
    )
    explanation_fallback_cases = sum(
        int((trace.get("explanations") or {}).get("fallback_count") or 0) > 0
        for trace in traces
    )
    return {
        "coverage": {
            "traced_cases": len(traces),
            "total_cases": len(results),
            "rate": round(len(traces) / len(results), 4) if results else None,
            "native_cases": origin_counts["native"],
            "legacy_synthesized_cases": origin_counts["legacy_synthesized"],
            "native_rate": round(origin_counts["native"] / len(results), 4)
            if results
            else None,
        },
        "lifecycle_counts": dict(sorted(lifecycle_counts.items())),
        "bottleneck_stage_counts": dict(sorted(bottleneck_counts.items())),
        "first_issue_layer_counts": dict(sorted(first_issue_layer_counts.items())),
        "first_issue_code_counts": dict(sorted(first_issue_code_counts.items())),
        "issue_code_counts": dict(sorted(issue_code_counts.items())),
        "source_unavailable_cases": source_unavailable_cases,
        "source_degraded_cases": source_degraded_cases,
        "source_uncertainty_kind_counts": dict(
            sorted(source_uncertainty_kind_counts.items())
        ),
        "source_uncertainty_case_counts": dict(
            sorted(source_uncertainty_case_counts.items())
        ),
        "explanation_fallback_cases": explanation_fallback_cases,
        "queue_wait_ms": _numeric_metric(
            (trace.get("queue") or {}).get("wait_ms") for trace in traces
        ),
        "stage_timings_ms": {
            stage: _numeric_metric(values) for stage, values in sorted(stage_values.items())
        },
        "models": {
            role: dict(sorted(counts.items())) for role, counts in sorted(model_counts.items())
        },
    }


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    case_counts = {
        status: sum(item["status"] == status for item in results)
        for status in ("PASS", "FAIL", "BLOCKED", "infrastructure_failure")
    }
    group_metrics: dict[str, dict[str, Any]] = {}
    all_assertions = [assertion for result in results for assertion in result.get("assertions", [])]
    for group in ASSERTION_GROUPS:
        assertions = [item for item in all_assertions if item.get("group") == group]
        group_metrics[group] = assertion_metric(assertions)

    overall_assertions = [item for item in all_assertions if item["name"] == "overall_status"]
    condition_assertions = [item for item in all_assertions if item["name"].startswith("condition_status:")]

    latencies = [
        float(item["latency_ms"])
        for item in results
        if isinstance(item.get("latency_ms"), (int, float))
    ]
    unknown_cases = sum(
        item.get("observed", {}).get("overall_status") == "无法判断" for item in results
    )
    layer_metrics: dict[str, dict[str, Any]] = {}
    for layer in LAYER_ORDER:
        assertions = [item for item in all_assertions if item.get("layer") == layer]
        statuses = [
            result.get("layers", {}).get(layer, {}).get("status", "NOT_EVALUATED")
            for result in results
        ]
        layer_metrics[layer] = {
            "label": LAYER_LABELS[layer],
            "cases": {
                status_name: sum(status == status_name for status in statuses)
                for status_name in ("PASS", "FAIL", "BLOCKED", "NOT_EVALUATED")
            },
            "assertions": assertion_metric(assertions),
        }
    first_failure_counts = Counter(
        str(item.get("first_failure_layer"))
        for item in results
        if item.get("first_failure_layer")
    )
    first_blocked_counts = Counter(
        str(item.get("first_blocked_layer"))
        for item in results
        if item.get("first_blocked_layer")
    )
    failure_code_counts = Counter(
        code
        for result in results
        for code in result.get("failure_codes", [])
        if code
    )
    return {
        "cases": {"total": len(results), **case_counts},
        "assertions": {
            "passed": sum(item["outcome"] == "PASS" for item in all_assertions),
            "failed": sum(item["outcome"] == "FAIL" for item in all_assertions),
            "blocked": sum(item["outcome"] == "BLOCKED" for item in all_assertions),
            "total": len(all_assertions),
        },
        "metrics": group_metrics,
        "overall_status_accuracy": assertion_metric(overall_assertions),
        "condition_status_accuracy": assertion_metric(condition_assertions),
        "routing_assertion_accuracy": assertion_metric(
            [item for item in all_assertions if item.get("group") == "routing"]
        ),
        "evidence_assertion_accuracy": assertion_metric(
            [item for item in all_assertions if item.get("group") == "evidence"]
        ),
        "layer_metrics": layer_metrics,
        "first_failure_layer_counts": dict(first_failure_counts),
        "first_blocked_layer_counts": dict(first_blocked_counts),
        "failure_code_counts": dict(failure_code_counts),
        "review_metrics": _review_metrics(results),
        "trace_metrics": _trace_metrics(results),
        "segment_metrics": _segment_metrics(results),
        "unknown_case_rate": round(unknown_cases / len(results), 4) if results else None,
        "latency_ms": {
            "count": len(latencies),
            "mean": round(statistics.fmean(latencies), 2) if latencies else None,
            "p50": percentile(latencies, 0.50),
            "p95": percentile(latencies, 0.95),
        },
    }


def build_payload(case: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    payload = dict(defaults)
    payload.update(case.get("patient") or {})
    payload.update(case.get("request_overrides") or {})
    payload["condition"] = case["condition"]
    return payload


def build_review_manifest(
    cases: list[dict[str, Any]], results: list[dict[str, Any]]
) -> dict[str, Any]:
    case_by_id = {str(case.get("id") or ""): case for case in cases}
    entries = []
    for result in results:
        case = case_by_id.get(str(result.get("case_id") or ""), {})
        review = result.get("review") if isinstance(result.get("review"), dict) else {}
        entries.append(
            {
                "case_id": result.get("case_id"),
                "title": result.get("title", ""),
                "category": list(case.get("category") or []),
                "condition": case.get("condition", ""),
                "evaluation_status": result.get("status"),
                "first_failure_layer": result.get("first_failure_layer"),
                "first_blocked_layer": result.get("first_blocked_layer"),
                "observed": result.get("observed") or {},
                "current_review": review,
                "review_template": {
                    "status": "pending",
                    "reviewed_by": "",
                    "reviewed_at": "",
                    "source_response_sha256": review.get("actual_response_sha256", ""),
                    "note": "",
                },
            }
        )
    return {
        "schema_version": "1.0.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "instructions": (
            "Review the saved raw response and evidence chain manually. "
            "Do not change gold labels automatically from this manifest."
        ),
        "cases": entries,
    }


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="病历智能筛选金标准评测器")
    parser.add_argument("--gold", type=Path, default=DEFAULT_GOLD, help="金标准 JSON 文件")
    parser.add_argument(
        "--endpoint",
        default="http://127.0.0.1:8000/api/medical/query",
        help="实时评测接口",
    )
    parser.add_argument("--replay", type=Path, help="单个响应文件、案例映射文件或响应目录")
    parser.add_argument("--case-id", action="append", help="只运行指定案例，可重复传入")
    parser.add_argument("--timeout", type=float, default=300.0, help="单案例 HTTP 超时秒数")
    parser.add_argument("--output", type=Path, help="评测报告输出路径")
    parser.add_argument("--response-dir", type=Path, help="实时模式下保存每个案例的原始响应")
    parser.add_argument(
        "--review-output",
        type=Path,
        help="输出待人工复核清单；不会自动修改金标准或标记 verified",
    )
    parser.add_argument(
        "--fail-on-assertion",
        action="store_true",
        help="断言失败或基础设施失败时返回非零退出码",
    )
    parser.add_argument(
        "--fail-on-review",
        action="store_true",
        help="存在未绑定或已过期的临床复核时返回非零退出码",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    gold = load_json(args.gold)
    validation_errors = validate_gold(gold)
    if validation_errors:
        detail = "\n".join(f"- {item}" for item in validation_errors)
        raise ValueError(f"无效金标准文件: {args.gold}\n{detail}")

    selected = set(args.case_id or [])
    cases = [case for case in gold["cases"] if not selected or case.get("id") in selected]
    missing = selected - {case.get("id") for case in cases}
    if missing:
        raise ValueError(f"未知案例ID: {', '.join(sorted(missing))}")

    results: list[dict[str, Any]] = []
    for case in cases:
        case_id = case["id"]
        if args.replay:
            response, latency_ms = replay_response(args.replay, case_id)
        else:
            payload = build_payload(case, gold.get("defaults") or {})
            response, latency_ms = post_json(args.endpoint, payload, args.timeout)
            if args.response_dir:
                write_json(args.response_dir / f"{case_id}.json", response)
        result = evaluate_case(case, response, latency_ms)
        results.append(result)
        failed_assertions = [
            item["name"]
            for item in result.get("assertions", [])
            if item["outcome"] == "FAIL"
        ]
        suffix = f" | FAIL: {', '.join(failed_assertions)}" if failed_assertions else ""
        if result.get("first_failure_layer"):
            suffix += f" | FIRST_LAYER: {result['first_failure_layer']}"
        elif result.get("first_blocked_layer"):
            suffix += f" | BLOCKED_LAYER: {result['first_blocked_layer']}"
        if latency_ms is None:
            print(f"[{result['status']}] {case_id}{suffix}")
        else:
            print(f"[{result['status']}] {case_id} | {latency_ms:.2f} ms{suffix}")

    report = {
        "schema_version": "1.2.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "replay" if args.replay else "live",
        "gold_file": str(args.gold.resolve()),
        "endpoint": None if args.replay else args.endpoint,
        "summary": summarize(results),
        "results": results,
    }
    output = args.output
    if output is None:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output = DEFAULT_REPORT_DIR / f"medical_filter_eval_{stamp}.json"
    write_json(output, report)
    if args.review_output:
        write_json(args.review_output, build_review_manifest(cases, results))
        print(f"复核清单: {args.review_output.resolve()}")
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    print(f"报告: {output.resolve()}")

    if args.fail_on_assertion:
        if report["summary"]["cases"]["infrastructure_failure"]:
            return 2
        if report["summary"]["cases"]["FAIL"]:
            return 1
    if args.fail_on_review:
        review_metrics = report["summary"]["review_metrics"]
        if review_metrics["unbound_or_stale_clinical_cases"]:
            return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
