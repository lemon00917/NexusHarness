"""Template-level candidate recall and constrained LLM reranking."""

from __future__ import annotations

import json
import re
from typing import Any, Protocol

from .similarity import best_similarity, normalize_text


class ChatClient(Protocol):
    def chat(self, messages: list[dict[str, str]], temperature: float = 0.1) -> str: ...


class _InvalidTemplateIdError(ValueError):
    """Raised when the model selects an ID outside the supplied candidates."""


_DOCUMENT_FAMILY_ALIASES = {
    "admission": ("入院记录", "住院志", "入院志", "入院病历"),
    "discharge": ("出院记录", "出院小结"),
    "operation": ("手术记录", "手术志"),
    "transfer": ("转科记录", "转科志"),
    "first_progress": ("首次病程记录", "首次病程"),
}


def _document_family(values: list[object]) -> str:
    """Return a deterministic clinical-document family for known aliases."""
    normalized_values = [normalize_text(value) for value in values if value]
    for family, aliases in _DOCUMENT_FAMILY_ALIASES.items():
        if any(
            normalize_text(alias) in value
            for value in normalized_values
            for alias in aliases
        ):
            return family
    return ""


def _extract_json_object(text: str) -> str:
    """Extract the first balanced JSON object without trusting the model prose."""
    start = text.find("{")
    if start < 0:
        raise ValueError("LLM response has no JSON object")

    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]

    # A truncated response has no balanced closing brace. Returning the
    # remaining object lets the repair path produce a useful parse error.
    return text[start:]


def _repair_json_text(text: str) -> str:
    """Apply conservative repairs commonly needed for small-model JSON output."""
    text = text.replace("\ufeff", "").replace("“", '"').replace("”", '"')

    # JSON does not allow raw control characters inside quoted strings. Keep
    # the model's content while escaping those characters instead of dropping
    # the whole recommendation batch.
    escaped_controls: list[str] = []
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            elif ord(char) < 0x20:
                escaped_controls.append({"\n": "\\n", "\r": "\\r", "\t": "\\t"}.get(char, ""))
                continue
        elif char == '"':
            in_string = True
        escaped_controls.append(char)
    repaired = "".join(escaped_controls)

    # Trailing commas are unambiguous and safe to remove.
    repaired = re.sub(r",\s*([}\]])", r"\1", repaired)

    # Small models sometimes omit the comma between two adjacent JSON values
    # or object members. Insert it only outside strings and only where the
    # previous token can end a value.
    output: list[str] = []
    in_string = False
    escaped = False
    for index, char in enumerate(repaired):
        output.append(char)
        can_end_value = False
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
                can_end_value = True
            else:
                continue
        elif char == '"':
            in_string = True
            continue
        else:
            can_end_value = char in '}]0123456789'
        if not can_end_value:
            continue
        next_index = index + 1
        while next_index < len(repaired) and repaired[next_index].isspace():
            next_index += 1
        if next_index >= len(repaired):
            continue
        next_char = repaired[next_index]
        if next_char not in '"[{-0123456789tfn':
            continue
        if next_index == index + 1 and char.isdigit():
            # Do not split a normal multi-digit or decimal number such as
            # 0.96 while looking for omitted delimiters.
            continue
        if output[-1] == ",":
            continue
        # Preserve the original whitespace, then add the missing delimiter.
        if next_index > index + 1:
            output.append(repaired[index + 1 : next_index])
        output.append(",")
        # The outer loop will append the whitespace and next token again; trim
        # the duplicated whitespace on the next iteration.
        if next_index > index + 1:
            del output[-2]
    return "".join(output)


def _json_object(value: str) -> dict[str, Any]:
    """Parse model JSON, accepting fenced/prose output and safe repairs."""
    text = str(value or "").strip()
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.IGNORECASE)
    if fenced:
        text = fenced.group(1).strip()

    candidate = _extract_json_object(text)
    errors: list[Exception] = []
    for item in (candidate, _repair_json_text(candidate)):
        try:
            parsed = json.loads(item)
        except (TypeError, ValueError) as exc:
            errors.append(exc)
            continue
        if not isinstance(parsed, dict):
            raise ValueError("LLM response root must be an object")
        return parsed

    detail = str(errors[-1]) if errors else "unknown JSON parse error"
    raise ValueError(f"LLM response is not valid JSON: {detail}") from errors[-1]


class TemplateMatcher:
    def __init__(self, *, top_k: int = 5, matched_threshold: float = 0.82, margin: float = 0.08) -> None:
        self.top_k = max(1, top_k)
        self.matched_threshold = matched_threshold
        self.margin = margin

    def match(
        self,
        *,
        html_template: dict[str, Any],
        html_nodes: list[dict[str, Any]],
        standard_templates: list[dict[str, Any]],
        existing_template_mappings: list[dict[str, Any]] | None = None,
        requested_standard_template_id: str | None = None,
        llm_client: ChatClient | None = None,
        existing_mapping_policy: str = "reference",
    ) -> dict[str, Any]:
        warnings: list[str] = []
        existing_mapping_policy = str(existing_mapping_policy or "reference").strip().lower()
        authoritative_existing = existing_mapping_policy == "authoritative"
        templates_by_id = {str(item.get("id")): item for item in standard_templates if item.get("id") is not None}
        existing_ids = {
            str(item.get("standard_xml_id"))
            for item in (existing_template_mappings or [])
            if item.get("standard_xml_id") is not None
        }

        if authoritative_existing and len(existing_ids) > 1:
            return {
                "status": "CONFLICT",
                "selected_template_id": None,
                "candidates": [],
                "warnings": ["同一 HTML 模板存在多个标准模板映射，禁止自动选择"],
            }

        if requested_standard_template_id:
            requested_id = str(requested_standard_template_id)
            requested = templates_by_id.get(requested_id)
            if requested is None:
                return {
                    "status": "FAILED",
                    "selected_template_id": None,
                    "candidates": [],
                    "warnings": ["指定的标准模板不属于可用临床文档模板候选集"],
                }
            conflict = bool(authoritative_existing and existing_ids and requested_id not in existing_ids)
            candidate = self._candidate(
                html_template,
                html_nodes,
                requested,
                authoritative_existing and requested_id in existing_ids,
            )
            candidate.update({"score": 1.0, "source": "manual", "reason": "用户明确指定标准模板"})
            if self._is_empty_standard_template(requested):
                return {
                    "status": "CONFLICT",
                    "selected_template_id": requested_id,
                    "candidates": [candidate],
                    "warnings": [
                        "The requested standard template has no standard nodes; automatic binding was stopped."
                    ],
                }
            return {
                "status": "REVIEW_REQUIRED" if conflict else "MATCHED",
                "selected_template_id": requested_id,
                "candidates": [candidate],
                "warnings": ["指定模板与数据库已有模板映射不一致，需要人工确认"] if conflict else [],
            }

        all_candidates = [
            self._candidate(
                html_template,
                html_nodes,
                item,
                authoritative_existing and str(item.get("id")) in existing_ids,
            )
            for item in standard_templates
            if item.get("id") is not None
        ]
        empty_candidates = [
            item for item in all_candidates if self._is_empty_standard_template(item)
        ]
        candidates = [
            item for item in all_candidates if not self._is_empty_standard_template(item)
        ]
        same_family_candidates = [
            item
            for item in candidates
            if item.get("features", {}).get("document_family_match") is True
        ]
        if same_family_candidates:
            candidates = same_family_candidates
        candidates.sort(key=lambda item: (-float(item["score"]), str(item["template_id"])))
        candidates = candidates[: self.top_k]
        if not candidates:
            return {
                "status": "CONFLICT",
                "selected_template_id": None,
                "candidates": [],
                "excluded_candidates": empty_candidates,
                "warnings": ["没有可用的标准临床文档模板候选"],
            }

        if authoritative_existing and existing_ids:
            existing_id = next(iter(existing_ids))
            existing = templates_by_id.get(existing_id)
            if existing is None:
                return {
                    "status": "CONFLICT",
                    "selected_template_id": None,
                    "candidates": candidates,
                    "warnings": ["已有映射指向不存在或非临床文档类型的标准模板"],
                }
            if self._is_empty_standard_template(existing):
                selected = self._candidate(html_template, html_nodes, existing, True)
                return {
                    "status": "CONFLICT",
                    "selected_template_id": existing_id,
                    "candidates": [selected],
                    "warnings": [
                        "An authoritative existing mapping points to a standard template with no nodes."
                    ],
                }
            selected = next((item for item in candidates if item["template_id"] == existing_id), None)
            if selected is None:
                selected = self._candidate(html_template, html_nodes, existing, True)
                candidates = [selected, *candidates[: self.top_k - 1]]
            selected.update({"score": 1.0, "source": "existing", "reason": "复用数据库已有模板映射"})
            candidates.sort(key=lambda item: item["template_id"] != existing_id)
            return {
                "status": "MATCHED",
                "selected_template_id": existing_id,
                "candidates": candidates,
                "warnings": warnings,
            }

        selected_id = str(candidates[0]["template_id"])
        if llm_client is not None and len(candidates) > 1:
            try:
                reranked = self._rerank_with_llm(html_template, html_nodes, candidates, llm_client)
                chosen = next(item for item in candidates if item["template_id"] == reranked["template_id"])
                chosen["score"] = round(float(chosen["score"]) * 0.65 + reranked["confidence"] * 0.35, 6)
                chosen["source"] = "rule+llm"
                chosen["reason"] = reranked["reason"] or chosen["reason"]
                candidates.sort(key=lambda item: item["template_id"] != chosen["template_id"])
                selected_id = chosen["template_id"]
            except Exception as exc:
                warnings.append(f"模板 LLM 重排失败，已保留规则排序：{exc}")

        top_score = float(candidates[0]["score"])
        second_score = float(candidates[1]["score"]) if len(candidates) > 1 else 0.0
        status = "MATCHED" if top_score >= self.matched_threshold and top_score - second_score >= self.margin else "REVIEW_REQUIRED"
        return {
            "status": status,
            "selected_template_id": selected_id,
            "candidates": candidates,
            "warnings": warnings,
        }

    def _candidate(
        self,
        html_template: dict[str, Any],
        html_nodes: list[dict[str, Any]],
        standard_template: dict[str, Any],
        existing: bool,
    ) -> dict[str, Any]:
        html_category = [html_template.get("category_name"), html_template.get("template_bdmcate_name")]
        html_names = [html_template.get("html_name"), *html_category]
        standard_category = [standard_template.get("category_name")]
        standard_names = [standard_template.get("name"), standard_template.get("desc"), *standard_category]
        sections = list(dict.fromkeys(str(node.get("section") or "") for node in html_nodes if node.get("section")))
        html_family = _document_family([*html_names, *sections])
        standard_family = _document_family([*standard_names])
        family_match = bool(html_family and standard_family and html_family == standard_family)
        category_score = best_similarity(html_category, standard_category)
        name_score = best_similarity(html_names, standard_names)
        section_score = best_similarity(sections, standard_names)
        if family_match:
            category_score = max(category_score, 1.0)
            name_score = max(name_score, 1.0)
        score = 1.0 if existing else round(category_score * 0.52 + name_score * 0.4 + section_score * 0.08, 6)
        reasons = []
        if family_match:
            reasons.append("临床文档类型别名一致")
        if category_score >= 0.85:
            reasons.append("分类名称高度相似")
        if name_score >= 0.85:
            reasons.append("模板名称高度相似")
        if section_score >= 0.75:
            reasons.append("HTML章节与模板语义相似")
        if not reasons:
            reasons.append("根据分类、名称和章节综合召回")
        return {
            "template_id": str(standard_template.get("id")),
            "category_id": str(standard_template.get("category_id") or ""),
            "category_name": str(standard_template.get("category_name") or ""),
            "template_name": str(standard_template.get("name") or ""),
            "standard_node_count": self._node_count(standard_template),
            "integrity": {
                "node_count": self._node_count(standard_template),
                "known": "node_count" in standard_template,
                "status": "EMPTY" if self._is_empty_standard_template(standard_template) else "AVAILABLE",
            },
            "score": score,
            "source": "existing" if existing else "rule",
            "reason": "；".join(reasons),
            "features": {
                "category_similarity": round(category_score, 4),
                "name_similarity": round(name_score, 4),
                "section_similarity": round(section_score, 4),
                "html_document_family": html_family,
                "standard_document_family": standard_family,
                "document_family_match": family_match,
            },
        }

    @staticmethod
    def _node_count(template: dict[str, Any]) -> int | None:
        value = template.get("node_count")
        if value is None:
            value = template.get("standard_node_count")
        if value is None and isinstance(template.get("integrity"), dict):
            value = template["integrity"].get("node_count")
        if value is None:
            return None
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return None

    @classmethod
    def _is_empty_standard_template(cls, template: dict[str, Any]) -> bool:
        # Older repository adapters may not expose node_count.  Unknown is
        # kept usable; only an explicit zero is treated as an integrity error.
        return cls._node_count(template) == 0

    def _rerank_with_llm(
        self,
        html_template: dict[str, Any],
        html_nodes: list[dict[str, Any]],
        candidates: list[dict[str, Any]],
        client: ChatClient,
    ) -> dict[str, Any]:
        allowed = {item["template_id"] for item in candidates}
        payload = {
            "html_template": {
                "category_name": html_template.get("category_name"),
                "html_name": html_template.get("html_name"),
                "sections": list(dict.fromkeys(node.get("section") for node in html_nodes if node.get("section")))[:30],
                "fields": list(dict.fromkeys(node.get("display_text") for node in html_nodes if node.get("display_text")))[:50],
            },
            "candidates": candidates,
        }
        response = client.chat(
            [
                {
                    "role": "system",
                    "content": "你是临床文档模板匹配器。只能从候选中选择，不得生成新ID。仅返回JSON对象。",
                },
                {
                    "role": "user",
                    "content": "选择最匹配的标准模板。返回selected_template_id、confidence(0到1)、reason。\n" + json.dumps(payload, ensure_ascii=False),
                },
            ],
            temperature=0.0,
        )
        try:
            return self._parse_rerank_response(response, allowed)
        except _InvalidTemplateIdError as first_error:
            # A small model may copy a template name or invent an ID even when
            # the initial prompt contains the full candidate objects. Retry
            # once with a compact, ID-only allowlist and keep strict validation.
            retry_candidates = [
                {
                    "standard_template_id": item["template_id"],
                    "template_name": item.get("template_name", ""),
                    "category_name": item.get("category_name", ""),
                }
                for item in candidates
            ]
            retry_response = client.chat(
                [
                    {
                        "role": "system",
                        "content": (
                            "你是临床文档模板匹配器。只能原样复制候选列表中的 "
                            "standard_template_id，绝对不能生成、改写或使用候选外 ID。"
                            "仅返回JSON对象。"
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            "上一轮返回了候选外 ID。请重新选择最匹配的标准模板。"
                            "selected_template_id 必须逐字等于下列某个 standard_template_id；"
                            "如果无法判断，也必须从候选列表中选择一个，不能返回模板名称。"
                            "返回selected_template_id、confidence(0到1)、reason。\n"
                            + json.dumps(
                                {
                                    "html_template": {
                                        "category_name": html_template.get("category_name"),
                                        "html_name": html_template.get("html_name"),
                                    },
                                    "candidates": retry_candidates,
                                    "allowed_standard_template_ids": [
                                        item["standard_template_id"] for item in retry_candidates
                                    ],
                                },
                                ensure_ascii=False,
                            )
                        ),
                    },
                ],
                temperature=0.0,
            )
            try:
                return self._parse_rerank_response(retry_response, allowed)
            except Exception as retry_error:
                raise ValueError(
                    f"{first_error}；候选 ID 纠正重试仍失败：{retry_error}"
                ) from retry_error

    @staticmethod
    def _normalize_template_id(value: Any) -> str:
        """Normalize harmless quoting without translating names into IDs."""
        if value is None or isinstance(value, (dict, list, tuple, set)):
            return ""
        return str(value).strip().strip("`\"'").strip()

    def _parse_rerank_response(
        self,
        response: str,
        allowed: set[str],
    ) -> dict[str, Any]:
        parsed = _json_object(response)
        raw_template_id = parsed.get("selected_template_id")
        template_id = self._normalize_template_id(raw_template_id)
        if template_id not in allowed:
            raise _InvalidTemplateIdError(
                "LLM 返回了候选集合外的标准模板 ID"
                f"（返回值：{raw_template_id!r}；允许值：{sorted(allowed)!r}）"
            )
        try:
            confidence = min(1.0, max(0.0, float(parsed.get("confidence", 0))))
        except (TypeError, ValueError):
            confidence = 0.0
        return {
            "template_id": template_id,
            "confidence": confidence,
            "reason": str(parsed.get("reason") or ""),
        }
