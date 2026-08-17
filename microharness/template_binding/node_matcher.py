"""Node-level deterministic matching with constrained semantic fallback."""

from __future__ import annotations

import json
import re
from contextvars import ContextVar
from typing import Any

from .similarity import best_similarity, best_similarity_normalized, label_variants, normalize_text, split_path
from .template_matcher import ChatClient, _json_object


_SELECTOR_SPLIT_RE = re.compile(r"[;,\s]+")
_SIMILARITY_TEXT_LIMIT = 320
_DIRECTIONAL_LABEL_PAIRS = (('入院', '出院'),)
_GENERIC_VALUE_NAMES = {"value", "text", "content", "item", "field", "值", "文本", "内容", "项目", "字段"}
_ACTIVE_FEATURES: ContextVar[tuple[dict[int, dict[str, Any]], dict[int, dict[str, Any]]] | None] = ContextVar(
    "template_binding_active_node_features", default=None
)


def _selector_set(value: object) -> set[str]:
    result: set[str] = set()
    for item in _SELECTOR_SPLIT_RE.split(str(value or "")):
        item = item.strip()
        if not item or item.casefold() == "null":
            continue
        result.add(item)
        if item.casefold().startswith(("code:", "scode:")):
            result.add(item.partition(":")[2])
        elif re.fullmatch(r"[A-Za-z][A-Za-z0-9_:-]*", item):
            result.add(f"code:{item}")
    return result


def _html_node_selectors(node: dict[str, Any]) -> set[str]:
    values: set[str] = set()
    for item in (
        *(node.get("selectors") or []),
        *(node.get("group_selectors") or []),
        *(node.get("scope_selectors") or []),
    ):
        values.update(_selector_set(item))
    for key in ("anchor_name", "node_key"):
        if node.get(key):
            values.update(_selector_set(node[key]))
    return values


def _bounded_text(value: object, limit: int = _SIMILARITY_TEXT_LIMIT) -> str:
    """Keep pairwise similarity bounded for large context/mapping fields."""
    text = str(value or "")
    if len(text) <= limit:
        return text
    head = max(1, (limit - 3) // 2)
    tail = max(1, limit - 3 - head)
    return f"{text[:head]}...{text[-tail:]}"


def _expand_equivalent_labels(values: tuple[str, ...]) -> set[str]:
    """Expand meaning-preserving forms generated from each label's shape."""
    expanded = set(values)
    for value in values:
        expanded.update(label_variants(value))
    return expanded


def _has_direction_conflict(left_values: tuple[str, ...], right_values: tuple[str, ...]) -> bool:
    left_text = '|'.join(left_values)
    right_text = '|'.join(right_values)
    for left_direction, right_direction in _DIRECTIONAL_LABEL_PAIRS:
        if (left_direction in left_text and right_direction in right_text
                and right_direction not in left_text and left_direction not in right_text):
            return True
        if (right_direction in left_text and left_direction in right_text
                and left_direction not in left_text and right_direction not in right_text):
            return True
    return False


class NodeMatcher:
    def __init__(
        self,
        *,
        top_k: int = 3,
        auto_threshold: float = 0.9,
        review_threshold: float = 0.58,
        semantic_candidate_k: int = 6,
        candidate_scan_k: int = 48,
        # Small local models are more reliable with short, independently
        # auditable batches than with one large JSON response.
        semantic_batch_size: int = 4,
        semantic_batch_limit: int | None = None,
        llm_weight: float = 0.7,
        llm_review_floor: float = 0.45,
        semantic_min_score: float = 0.1,
        semantic_ambiguity_margin: float = 0.12,
    ) -> None:
        self.top_k = max(1, top_k)
        self.auto_threshold = auto_threshold
        self.review_threshold = review_threshold
        self.semantic_candidate_k = max(1, semantic_candidate_k)
        self.candidate_scan_k = max(self.semantic_candidate_k, self.top_k, candidate_scan_k)
        self.semantic_batch_size = max(1, semantic_batch_size)
        self.semantic_batch_limit = (
            None
            if semantic_batch_limit is None or semantic_batch_limit <= 0
            else max(1, semantic_batch_limit)
        )
        self.llm_weight = min(1.0, max(0.0, float(llm_weight)))
        # A constrained LLM choice is useful as a human-review suggestion
        # even when its blended score is below the automatic review cutoff.
        self.llm_review_floor = min(1.0, max(0.0, float(llm_review_floor)))
        self.semantic_min_score = min(1.0, max(0.0, float(semantic_min_score)))
        self.semantic_ambiguity_margin = min(1.0, max(0.0, float(semantic_ambiguity_margin)))

    def match(
        self,
        *,
        standard_nodes: list[dict[str, Any]],
        html_nodes: list[dict[str, Any]],
        existing_node_mappings: list[dict[str, Any]] | None = None,
        llm_client: ChatClient | None = None,
        existing_mapping_policy: str = "reference",
    ) -> dict[str, Any]:
        warnings: list[str] = []
        existing_mapping_policy = str(existing_mapping_policy or "reference").strip().lower()
        authoritative_existing = existing_mapping_policy == "authoritative"
        llm_attempted = 0
        llm_selected = 0
        llm_error = False
        bindable = [node for node in standard_nodes if node.get("bindable")]
        matchable_html_nodes = [
            node
            for node in html_nodes
            if node.get("node_key") and not bool(node.get("structural"))
        ]
        html_by_key = {
            str(node.get("node_key")): node for node in matchable_html_nodes
        }
        standard_by_id = {str(node.get("id")): node for node in bindable if node.get("id")}
        existing_reference = self._existing_recommendations(
            existing_node_mappings or [], standard_by_id, matchable_html_nodes, warnings
        )
        existing = existing_reference if authoritative_existing else []
        reserved_html = {key for item in existing for key in item["html_node_keys"]}
        pools: dict[str, list[dict[str, Any]]] = {}
        decision_details: dict[str, dict[str, Any]] = {}

        standard_features = {
            id(node): self._prepare_standard_features(node) for node in bindable
        }
        html_features = {
            id(node): self._prepare_html_features(node) for node in matchable_html_nodes
        }
        feature_token = _ACTIVE_FEATURES.set((standard_features, html_features))
        try:
            existing_standard_ids = {item["standard_node_id"] for item in existing}
            full_score_pair_count = 0
            for standard_node in bindable:
                standard_id = str(standard_node["id"])
                if standard_id in existing_standard_ids:
                    continue
                candidate_html_nodes = self._candidate_html_nodes(
                    standard_node,
                    matchable_html_nodes,
                    standard_features,
                    html_features,
                    reserved_html,
                    len(bindable),
                )
                full_score_pair_count += len(candidate_html_nodes)
                ranked = [
                    self._score_pair(
                        standard_node,
                        html_node,
                        len(bindable),
                        len(matchable_html_nodes),
                    )
                    for html_node in candidate_html_nodes
                    if str(html_node.get("node_key")) not in reserved_html
                ]
                ranked.sort(key=lambda item: (-item["score"], item["html_node_key"]))
                if llm_client is None:
                    ranked = [item for item in ranked if item["score"] >= 0.25]
                    pools[standard_id] = ranked[: self.top_k]
                else:
                    # Keep a wider, still fully controlled candidate set for the
                    # semantic model. The model may only select from these nodes.
                    pools[standard_id] = ranked[: self.semantic_candidate_k]
        finally:
            _ACTIVE_FEATURES.reset(feature_token)

        llm_choices: dict[str, dict[str, Any]] = {}
        semantic_evaluations = {
            standard_id: self._semantic_eligibility(candidates)
            for standard_id, candidates in pools.items()
        }
        semantic_ids = []
        if llm_client is not None:
            semantic_ids = [
                standard_id
                for standard_id, evaluation in semantic_evaluations.items()
                if evaluation[0]
            ]
        semantic_eligible_ids = set(semantic_ids)
        if self.semantic_batch_limit is not None:
            semantic_ids = semantic_ids[: self.semantic_batch_limit]
        semantic_id_set = set(semantic_ids)
        semantic_batch_count = 0
        if llm_client is not None and semantic_ids:
            llm_attempted = len(semantic_ids)
            for start in range(0, len(semantic_ids), self.semantic_batch_size):
                batch_ids = semantic_ids[start : start + self.semantic_batch_size]
                semantic_batch_count += 1
                try:
                    batch_choices = self._semantic_rerank(
                        batch_ids, standard_by_id, html_by_key, pools, llm_client
                    )
                    llm_choices.update(batch_choices)
                except Exception as exc:
                    llm_error = True
                    warnings.append(
                        f"节点 LLM 匹配批次 {semantic_batch_count} 失败，已保留规则结果：{exc}"
                    )
            llm_selected = len(llm_choices)

        proposed: list[dict[str, Any]] = []
        llm_applied_count = 0
        for standard_id, candidates in pools.items():
            semantic_eligible, semantic_reason = semantic_evaluations.get(
                standard_id, (False, "NO_CANDIDATES")
            )
            decision_details[standard_id] = {
                "candidate_count": len(candidates),
                "semantic_eligible": semantic_eligible,
                "semantic_eligibility_reason": semantic_reason,
                "semantic_scheduled": standard_id in semantic_id_set,
                "semantic_batch_limited": (
                    standard_id in semantic_eligible_ids and standard_id not in semantic_id_set
                ),
                "llm_selected": False,
                "chosen_score": None,
            }
            if not candidates:
                continue
            chosen = candidates[0]
            source = "rule"
            reason = chosen["reason"]
            llm_choice = llm_choices.get(standard_id)
            llm_reviewable = False
            if llm_choice:
                selected = next(
                    (item for item in candidates if item["html_node_key"] == llm_choice["html_node_key"]),
                    None,
                )
                if selected is not None and not self._direction_conflict(standard_by_id[standard_id], html_by_key[llm_choice['html_node_key']]):
                    chosen = dict(selected)
                    rule_score = float(chosen["score"])
                    llm_confidence = float(llm_choice["confidence"])
                    llm_reviewable = llm_confidence >= self.llm_review_floor
                    semantic_score = (
                        llm_confidence
                        if llm_confidence >= self.auto_threshold
                        else (
                            rule_score * (1.0 - self.llm_weight)
                            + llm_confidence * self.llm_weight
                        )
                    )
                    # Semantic evidence can promote a weak lexical candidate,
                    # but it must never lower a stronger deterministic score.
                    chosen["score"] = round(max(rule_score, semantic_score), 6)
                    source = "rule+llm"
                    reason = llm_choice["reason"] or chosen["reason"]
                    llm_applied_count += 1
                    decision_details[standard_id]["llm_selected"] = True
            decision_details[standard_id]["chosen_score"] = float(chosen["score"])
            # Keep a valid, controlled LLM selection for the reviewer instead
            # of reporting it as unmatched solely because blending lowered the
            # score. It is still never auto-accepted below review_threshold.
            if chosen["score"] < self.review_threshold and not llm_reviewable:
                continue
            if self._direction_conflict(standard_by_id[standard_id], html_by_key[chosen['html_node_key']]):
                continue
            proposed.append(
                {
                    "standard_node_id": standard_id,
                    "standard_node_name": self._standard_name(standard_by_id[standard_id]),
                    "standard_path": str(standard_by_id[standard_id].get("path_text") or ""),
                    "html_node_keys": [chosen["html_node_key"]],
                    "html_node_name": chosen["html_node_name"],
                    "html_selectors": chosen["html_selectors"],
                    "mapping_values": chosen["mapping_values"],
                    "confidence": round(float(chosen["score"]), 4),
                    "source": source,
                    "status": "AUTO" if chosen["score"] >= self.auto_threshold else "REVIEW_REQUIRED",
                    "reason": reason,
                    "candidates": candidates,
                }
            )

        accepted: list[dict[str, Any]] = list(existing)
        conflicted_ids: set[str] = set()
        for item in sorted(proposed, key=lambda row: (-row["confidence"], row["standard_node_id"])):
            html_keys = set(item["html_node_keys"])
            if html_keys & reserved_html:
                conflicted_ids.add(item["standard_node_id"])
                continue
            accepted.append(item)
            reserved_html.update(html_keys)

        accepted.sort(key=lambda item: self._standard_order(standard_by_id.get(item["standard_node_id"], {})))
        matched_ids = {item["standard_node_id"] for item in accepted}
        unmatched: list[dict[str, Any]] = []
        unmatched_reason_counts: dict[str, int] = {}
        for node in bindable:
            standard_id = str(node["id"])
            if standard_id in matched_ids:
                continue
            detail = decision_details.get(standard_id, {})
            candidate_count = int(detail.get("candidate_count") or 0)
            if standard_id in conflicted_ids:
                reason_code = "HTML_NODE_CONFLICT"
                reason = "候选HTML节点已被更高置信度映射占用"
            elif candidate_count == 0:
                reason_code = "NO_AVAILABLE_HTML"
                reason = "当前HTML解析结果没有可匹配节点"
            elif detail.get("llm_selected") and (
                float(detail.get("chosen_score") or 0) < self.review_threshold
            ):
                reason_code = "LLM_SELECTED_BELOW_REVIEW"
                reason = "LLM选择的候选综合分低于人工复核阈值"
            elif detail.get("semantic_eligible") and llm_client is None:
                reason_code = "LLM_DISABLED"
                reason = "LLM semantic review is disabled; the deterministic candidate did not reach the review threshold"
            elif detail.get("semantic_batch_limited"):
                reason_code = "SEMANTIC_BATCH_LIMIT"
                reason = "候选符合语义匹配条件，但超过本次语义匹配批次上限"
            elif detail.get("semantic_scheduled") and not detail.get("llm_selected"):
                reason_code = "LLM_NO_SELECTION"
                reason = "LLM未从受控候选集合中选择节点"
            elif detail.get("semantic_eligibility_reason") == "BELOW_SEMANTIC_MIN_SCORE":
                reason_code = "BELOW_SEMANTIC_MIN_SCORE"
                reason = "规则候选分低于语义召回最低要求"
            elif detail.get("semantic_eligibility_reason") == "PLAUSIBLE_SEMANTIC_CANDIDATE":
                reason_code = "LLM_NO_SELECTION"
                reason = "候选存在可解释的语义可能性，但LLM未确认对应关系"
            else:
                reason_code = "BELOW_REVIEW_THRESHOLD"
                reason = "规则与语义综合分低于人工复核阈值"
            unmatched_reason_counts[reason_code] = unmatched_reason_counts.get(reason_code, 0) + 1
            unmatched.append(
                {
                    "standard_node_id": standard_id,
                    "standard_node_name": self._standard_name(node),
                    "standard_path": str(node.get("path_text") or ""),
                    "reason_code": reason_code,
                    "reason": reason,
                    "candidate_count": candidate_count,
                }
            )
        review_count = sum(item["status"] == "REVIEW_REQUIRED" for item in accepted)
        status = "REVIEW_REQUIRED" if review_count or unmatched else "COMPLETED"
        return {
            "status": status,
            "mappings": accepted,
            "mapping_count": len(accepted),
            "existing_count": sum(item["status"] == "EXISTING" for item in accepted),
            "auto_count": sum(item["status"] == "AUTO" for item in accepted),
            "review_count": review_count,
            "unmatched_count": len(unmatched),
            "unmatched": unmatched,
            "existing_reference_mappings": existing_reference,
            "reference_existing_count": len(existing_reference),
            "llm": {
                "enabled": llm_client is not None,
                "attempted": llm_attempted,
                "selected": llm_selected,
                "used": llm_selected > 0,
                "error": llm_error,
                "mode": "semantic_rerank" if llm_attempted else ("disabled" if llm_client is None else "rule_only"),
            },
            "diagnostics": {
                "candidate_pool_size": self.semantic_candidate_k if llm_client is not None else self.top_k,
                "semantic_candidate_count": len(semantic_ids),
                "semantic_batch_count": semantic_batch_count,
                "llm_api_call_count": semantic_batch_count,
                "llm_applied_count": llm_applied_count,
                "semantic_min_score": self.semantic_min_score,
                "semantic_ambiguity_margin": self.semantic_ambiguity_margin,
                "llm_review_floor": self.llm_review_floor,
                "candidate_scan_k": self.candidate_scan_k,
                "possible_pair_count": len(bindable) * len(matchable_html_nodes),
                "full_score_pair_count": full_score_pair_count,
                "unmatched_reason_counts": unmatched_reason_counts,
            },
            "warnings": warnings,
        }

    def _semantic_eligible(self, candidates: list[dict[str, Any]]) -> bool:
        return self._semantic_eligibility(candidates)[0]

    def _semantic_eligibility(self, candidates: list[dict[str, Any]]) -> tuple[bool, str]:
        """Return whether a candidate pool needs semantic review.

        Deterministic matches with a clear score lead are left alone.  LLM
        review is reserved for ambiguous or plausible-but-weak pools, which
        keeps large templates bounded without making the matcher depend on a
        document-specific allowlist.
        """
        if not candidates:
            return False, "NO_CANDIDATES"
        top_score = float(candidates[0].get("score") or 0.0)
        if top_score >= self.auto_threshold:
            return False, "AUTO_THRESHOLD_REACHED"
        # A caller can explicitly lower the review threshold to request
        # semantic processing for all candidate pools (useful for review and
        # controlled evaluation runs).
        if self.review_threshold <= 0.5:
            return True, "FORCED_REVIEW"
        if top_score < self.semantic_min_score:
            return False, "BELOW_SEMANTIC_MIN_SCORE"
        second_score = float(candidates[1].get("score") or 0.0) if len(candidates) > 1 else 0.0
        if top_score - second_score <= self.semantic_ambiguity_margin:
            return True, "AMBIGUOUS_CANDIDATES"
        # A clear lexical lead is still not proof of semantic equivalence.
        # Send every plausible, non-automatic lead through the same controlled
        # semantic check so unseen clinical synonyms do not depend on a local
        # alias allowlist.
        return True, "PLAUSIBLE_SEMANTIC_CANDIDATE"

    def _existing_recommendations(
        self,
        rows: list[dict[str, Any]],
        standard_by_id: dict[str, dict[str, Any]],
        html_nodes: list[dict[str, Any]],
        warnings: list[str],
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for row in rows:
            standard_id = str(row.get("standard_node_id") or "")
            standard_node = standard_by_id.get(standard_id)
            if standard_node is None:
                warnings.append(f"已有节点映射 {row.get('id')} 的标准节点不属于当前模板，已忽略")
                continue
            selectors = set()
            for value in (row.get("html_node_id"), row.get("html_node_code")):
                selectors.update(_selector_set(value))
            matched_html = [node for node in html_nodes if selectors & _html_node_selectors(node)]
            if not matched_html:
                warnings.append(f"已有节点映射 {row.get('id')} 未匹配到当前 HTML 解析节点，需要复核")
                continue
            result.append(
                {
                    "standard_node_id": standard_id,
                    "standard_node_name": self._standard_name(standard_node),
                    "standard_path": str(standard_node.get("path_text") or ""),
                    "html_node_keys": [str(node["node_key"]) for node in matched_html],
                    "html_node_name": "；".join(self._html_name(node) for node in matched_html),
                    "html_selectors": sorted(selectors),
                    "mapping_values": str(row.get("mapping_values") or ""),
                    "confidence": 1.0,
                    "source": "existing",
                    "status": "EXISTING",
                    "reason": "复用数据库已有节点映射",
                    "existing_mapping_id": str(row.get("id") or ""),
                    "candidates": [],
                }
            )
        return result

    @classmethod
    def _prepare_standard_features(cls, node: dict[str, Any]) -> dict[str, Any]:
        path_labels = tuple(split_path(node.get('path_text')))
        semantic_labels = cls._standard_semantic_labels(node)
        name_labels = (
            *semantic_labels,
            node.get('mapping_value'),
        )
        parent_labels = tuple(
            value
            for value in path_labels[:-1]
            if normalize_text(value) not in _GENERIC_VALUE_NAMES
        )
        context_labels = (
            _bounded_text(node.get('description')),
            _bounded_text(node.get('node_value')),
        )
        selector_labels = (
            node.get('mapping_value'),
            node.get('node_en'),
        )
        try:
            order = int(node.get('order') or 0)
        except (TypeError, ValueError):
            order = 0
        normalized_names = tuple(normalize_text(value) for value in name_labels if value)
        normalized_paths = tuple(normalize_text(value) for value in path_labels if value)
        normalized_parents = tuple(normalize_text(value) for value in parent_labels if value)
        normalized_contexts = tuple(normalize_text(value) for value in context_labels if value)
        normalized_selectors = tuple(normalize_text(value) for value in selector_labels if value)
        normalized_equivalent_names = _expand_equivalent_labels(normalized_names)
        normalized_exact_set = normalized_equivalent_names | set(normalized_selectors)
        return {
            'normalized_name_labels': normalized_names,
            'normalized_name_set': set(normalized_names),
            'normalized_equivalent_name_set': normalized_equivalent_names,
            'normalized_path_labels': normalized_paths,
            'normalized_parent_labels': normalized_parents,
            'normalized_context_labels': normalized_contexts,
            'normalized_selector_labels': normalized_selectors,
            'normalized_exact_set': normalized_exact_set,
            'recall_chars': frozenset(''.join(normalized_exact_set)),
            'path_chars': frozenset(''.join(normalized_paths)),
            'order': order,
        }

    @staticmethod
    def _prepare_html_features(node: dict[str, Any]) -> dict[str, Any]:
        labels = (
            node.get('placeholder'),
            node.get('display_text'),
            node.get('local_label'),
            node.get('mapping_value'),
            node.get('anchor_name'),
        )
        group_labels = tuple(node.get('group_labels') or ())
        anchor_path = tuple(node.get('anchor_path') or ())
        selectors = tuple(node.get('selectors') or ()) + tuple(node.get('group_selectors') or ())
        try:
            order = int(node.get('order') or 0)
        except (TypeError, ValueError):
            order = 0
        normalized_labels = tuple(normalize_text(value) for value in labels if value)
        normalized_section = tuple(
            normalize_text(value)
            for value in (node.get('section'), *group_labels, *anchor_path)
            if value
        )
        normalized_group_labels = tuple(normalize_text(value) for value in group_labels if value)
        normalized_context = (normalize_text(_bounded_text(node.get('context_text'))),)
        normalized_selectors = tuple(normalize_text(value) for value in selectors if value)
        normalized_equivalent_labels = _expand_equivalent_labels(normalized_labels)
        normalized_exact_set = normalized_equivalent_labels | set(normalized_selectors)
        return {
            'normalized_labels': normalized_labels,
            'normalized_label_set': set(normalized_labels),
            'normalized_equivalent_label_set': normalized_equivalent_labels,
            'normalized_section': normalized_section,
            'normalized_group_labels': normalized_group_labels,
            'normalized_context': normalized_context,
            'normalized_selectors': normalized_selectors,
            'normalized_exact_set': normalized_exact_set,
            'recall_chars': frozenset(''.join(normalized_exact_set)),
            'path_chars': frozenset(''.join((*normalized_section, *normalized_group_labels))),
            'order': order,
        }

    def _candidate_html_nodes(
        self,
        standard_node: dict[str, Any],
        html_nodes: list[dict[str, Any]],
        standard_features: dict[int, dict[str, Any]],
        html_features: dict[int, dict[str, Any]],
        reserved_html: set[str],
        standard_count: int,
    ) -> list[dict[str, Any]]:
        available = [
            node
            for node in html_nodes
            if str(node.get('node_key') or '') not in reserved_html
            and self._section_compatible(standard_node, node)
        ]
        if len(available) <= self.candidate_scan_k:
            return available

        standard_feature = standard_features[id(standard_node)]
        standard_position = standard_feature['order'] / max(1, standard_count)
        ranked: list[tuple[int, float, float, str, dict[str, Any]]] = []
        for html_node in available:
            html_feature = html_features[id(html_node)]
            exact = bool(
                standard_feature['normalized_exact_set']
                & html_feature['normalized_exact_set']
            )
            name_overlap = self._character_overlap(
                standard_feature['recall_chars'], html_feature['recall_chars']
            )
            path_overlap = self._character_overlap(
                standard_feature['path_chars'], html_feature['path_chars']
            )
            html_position = html_feature['order'] / max(1, len(html_nodes))
            order_distance = abs(standard_position - html_position)
            recall_score = name_overlap * 0.82 + path_overlap * 0.13 + (1.0 - order_distance) * 0.05
            ranked.append(
                (
                    1 if exact else 0,
                    recall_score,
                    -order_distance,
                    str(html_node.get('node_key') or ''),
                    html_node,
                )
            )
        ranked.sort(key=lambda item: (-item[0], -item[1], -item[2], item[3]))
        return [item[4] for item in ranked[: self.candidate_scan_k]]

    @staticmethod
    def _section_compatible(
        standard_node: dict[str, Any], html_node: dict[str, Any]
    ) -> bool:
        """Reject explicit cross-section candidates for top-level section values."""
        path = split_path(standard_node.get("path_text"))
        if len(path) != 3:
            return True
        leaf = normalize_text(path[-1])
        if leaf not in {"text", "content", "value", "field", "文本", "内容", "值", "字段"}:
            return True
        expected_forms = set(label_variants(path[-2]))
        if not expected_forms:
            return True
        actual_labels = [
            html_node.get("section"),
            *(html_node.get("group_labels") or []),
        ]
        actual_forms = {
            form
            for label in actual_labels
            if label
            for form in label_variants(label)
        }
        if not actual_forms or expected_forms & actual_forms:
            return True
        # Keep genuine clinical aliases such as "入院情况" and
        # "入院病情及诊治经过", while rejecting unrelated sections.
        return best_similarity([path[-2]], actual_labels) >= 0.35

    @staticmethod
    def _character_overlap(left: frozenset[str], right: frozenset[str]) -> float:
        if not left or not right:
            return 0.0
        intersection = len(left & right)
        if intersection == 0:
            return 0.0
        containment = intersection / min(len(left), len(right))
        union = len(left | right)
        jaccard = intersection / union if union else 0.0
        return max(containment * 0.9, jaccard)

    @staticmethod
    def _direction_conflict(standard_node: dict[str, Any], html_node: dict[str, Any]) -> bool:
        standard_values = (
            *NodeMatcher._standard_semantic_labels(standard_node),
            *split_path(standard_node.get('path_text')),
        )
        html_values = tuple(
            str(value)
            for value in (
                html_node.get('placeholder'),
                html_node.get('display_text'),
                html_node.get('local_label'),
                html_node.get('section'),
                *(html_node.get('group_labels') or []),
            )
            if value
        )
        return _has_direction_conflict(standard_values, html_values)

    def _score_pair(
        self,
        standard_node: dict[str, Any],
        html_node: dict[str, Any],
        standard_count: int,
        html_count: int,
    ) -> dict[str, Any]:
        active_features = _ACTIVE_FEATURES.get()
        if active_features is None:
            standard_features = self._prepare_standard_features(standard_node)
            html_features = self._prepare_html_features(html_node)
        else:
            standard_features = active_features[0].get(id(standard_node)) or self._prepare_standard_features(standard_node)
            html_features = active_features[1].get(id(html_node)) or self._prepare_html_features(html_node)
        name_score = best_similarity_normalized(
            standard_features["normalized_name_labels"], html_features["normalized_labels"]
        )
        path_score = best_similarity_normalized(
            standard_features["normalized_path_labels"], html_features["normalized_section"]
        )
        group_score = best_similarity_normalized(
            standard_features["normalized_parent_labels"], html_features["normalized_group_labels"]
        )
        context_score = best_similarity_normalized(
            standard_features["normalized_context_labels"], html_features["normalized_context"]
        )
        selector_score = best_similarity_normalized(
            standard_features["normalized_selector_labels"], html_features["normalized_selectors"]
        )
        standard_order = standard_features["order"]
        html_order = html_features["order"]
        standard_position = standard_order / max(1, standard_count)
        html_position = html_order / max(1, html_count)
        order_score = max(0.0, 1.0 - abs(standard_position - html_position))
        exact = bool(standard_features["normalized_name_set"] & html_features["normalized_label_set"])
        equivalent = bool(
            standard_features['normalized_equivalent_name_set']
            & html_features['normalized_equivalent_label_set']
        )
        direction_conflict = _has_direction_conflict(
            (
                *standard_features['normalized_name_labels'],
                *standard_features['normalized_path_labels'],
                *standard_features['normalized_parent_labels'],
            ),
            (
                *html_features['normalized_labels'],
                *html_features['normalized_section'],
                *html_features['normalized_group_labels'],
            ),
        )
        score = (
            name_score * 0.5
            + path_score * 0.2
            + group_score * 0.12
            + context_score * 0.1
            + selector_score * 0.05
            + order_score * 0.03
        )
        if exact:
            score = max(score, 0.94)
        elif equivalent:
            score = max(score, 0.92)
        if direction_conflict:
            score *= 0.2
        reasons = []
        if direction_conflict:
            reasons.append('入院/出院业务方向冲突')
        elif exact:
            reasons.append("字段名称精确一致")
        elif equivalent:
            reasons.append('字段表示形式归一后一致')
        elif name_score >= 0.8:
            reasons.append("字段名称高度相似")
        if path_score >= 0.75:
            reasons.append("章节路径相似")
        if group_score >= 0.75:
            reasons.append("父级业务语义一致")
        if context_score >= 0.75:
            reasons.append("上下文语义相似")
        if not reasons:
            reasons.append("根据名称、章节、上下文和顺序综合评分")
        return {
            "html_node_key": str(html_node.get("node_key") or ""),
            "html_node_name": self._html_name(html_node),
            "html_selectors": sorted(_html_node_selectors(html_node) - {str(html_node.get("node_key") or "")}),
            "mapping_values": str(html_node.get("mapping_value") or ""),
            "score": round(score, 6),
            "semantic_hint": False,
            "reason": "；".join(reasons),
        }

    def _semantic_rerank(
        self,
        standard_ids: list[str],
        standard_by_id: dict[str, dict[str, Any]],
        html_by_key: dict[str, dict[str, Any]],
        pools: dict[str, list[dict[str, Any]]],
        client: ChatClient,
    ) -> dict[str, dict[str, Any]]:
        allowed = {
            standard_id: {candidate["html_node_key"] for candidate in pools[standard_id]}
            for standard_id in standard_ids
        }
        payload = []
        for standard_id in standard_ids:
            node = standard_by_id[standard_id]
            payload.append(
                {
                    "standard_node_id": standard_id,
                    "standard_name": self._standard_name(node),
                    "standard_name_forms": list(label_variants(self._standard_name(node))),
                    "standard_path": self._truncate(node.get("path_text"), 240),
                    "node_cn": self._truncate(node.get("node_cn"), 120),
                    "node_en": self._truncate(node.get("node_en"), 120),
                    "node_value": self._truncate(node.get("node_value"), 320),
                    "description": self._truncate(node.get("description"), 320),
                    "mapping_value": self._truncate(node.get("mapping_value"), 160),
                    "node_role": self._truncate(node.get("node_role"), 40),
                    "candidates": [
                        {
                            "html_node_key": candidate["html_node_key"],
                            "name": self._truncate(candidate["html_node_name"], 160),
                            "name_forms": list(label_variants(candidate["html_node_name"])),
                            "placeholder": self._truncate(
                                html_by_key[candidate["html_node_key"]].get("placeholder"), 160
                            ),
                            "local_label": self._truncate(
                                html_by_key[candidate["html_node_key"]].get("local_label"), 160
                            ),
                            "section": self._truncate(
                                html_by_key[candidate["html_node_key"]].get("section"), 160
                            ),
                            "group_labels": [
                                self._truncate(value, 120)
                                for value in (
                                    html_by_key[candidate["html_node_key"]].get("group_labels") or []
                                )
                            ],
                            "anchor_path": [
                                self._truncate(value, 80)
                                for value in (
                                    html_by_key[candidate["html_node_key"]].get("anchor_path") or []
                                )
                            ],
                            "selectors": [
                                self._truncate(value, 100)
                                for value in (
                                    html_by_key[candidate["html_node_key"]].get("selectors") or []
                                )[:8]
                            ],
                            "context": self._truncate(
                                html_by_key[candidate["html_node_key"]].get("context_text"), 240
                            ),
                            "rule_score": candidate["score"],
                        }
                        for candidate in pools[standard_id]
                    ],
                }
            )
        messages = self._strict_semantic_messages(payload)
        response = client.chat(messages, temperature=0.0)
        try:
            parsed = self._parse_semantic_response(response, allowed)
            if not standard_ids:
                return parsed

            unresolved_ids = [
                standard_id for standard_id in standard_ids if standard_id not in parsed
            ]
            if not unresolved_ids:
                return parsed

            # A small model can return valid but empty or partial JSON for a
            # large batch. Retry each unresolved standard node separately so
            # an obvious synonym is not lost because of unrelated candidates.
            payload_by_id = {
                str(item.get("standard_node_id") or ""): item for item in payload
            }
            result, _, _ = self._semantic_retry_each(
                unresolved_ids, payload_by_id, allowed, client
            )
            result = {**parsed, **result}
            return result
        except Exception as first_error:
            # Do not replay the same large batch: if the model omitted a comma
            # or hit its output limit, an identical retry usually fails again.
            # Split the request into one standard node per retry so successful
            # choices can still be used and the remaining nodes fall back to
            # deterministic matching independently.
            payload_by_id = {
                str(item.get("standard_node_id") or ""): item for item in payload
            }
            recovered, parsed_any, retry_errors = self._semantic_retry_each(
                standard_ids, payload_by_id, allowed, client
            )
            if parsed_any:
                return recovered
            retry_detail = "; ".join(retry_errors[-2:]) or "逐节点重试没有返回可解析结果"
            raise ValueError(
                f"批量响应解析失败：{first_error}；逐节点重试失败：{retry_detail}"
            ) from first_error

    def _semantic_retry_each(
        self,
        standard_ids: list[str],
        payload_by_id: dict[str, dict[str, Any]],
        allowed: dict[str, set[str]],
        client: ChatClient,
    ) -> tuple[dict[str, dict[str, Any]], bool, list[str]]:
        """Retry semantic choices one standard node at a time.

        The boolean distinguishes a valid empty response from a transport or
        JSON failure. A valid empty response means the model reviewed the node
        and found no acceptable candidate; it should not create a batch error.
        """
        result: dict[str, dict[str, Any]] = {}
        parsed_any = False
        errors: list[str] = []
        for standard_id in standard_ids:
            item = payload_by_id.get(standard_id)
            if item is None:
                continue
            single_allowed = {standard_id: allowed.get(standard_id, set())}
            try:
                retry_response = client.chat(
                    self._strict_semantic_messages([item]), temperature=0.0
                )
                parsed = self._parse_semantic_response(retry_response, single_allowed)
            except Exception as exc:
                errors.append(f"{standard_id}: {exc}")
                continue
            parsed_any = True
            result.update(parsed)
        return result, parsed_any, errors

    @staticmethod
    def _strict_semantic_messages(payload: list[dict[str, Any]]) -> list[dict[str, str]]:
        """Build a small-model-safe prompt with explicit clinical guardrails."""
        return [
            {
                "role": "system",
                "content": (
                    "You are a strict clinical document HTML binding reviewer.\n"
                    "Return only one valid JSON object; no Markdown, comments, or extra text.\n"
                    "The standard node may select only one html_node_key from its own candidates.\n"
                    "Never invent IDs, return null IDs, return display text, or select a key from another item.\n"
                    "Use the standard path and clinical purpose first. node_value and description explain the target meaning; "
                    "they are not evidence that the same words must appear in HTML.\n"
                    "Treat section and group_labels as hard clinical context. A top-level standard section must not use a candidate from a different explicit HTML section.\n"
                    "Do not select a broad parent or summary range just because its scope text contains a child label. "
                    "For example, never bind standard section '辅助检查' to HTML section '病历摘要' only because the summary text contains '辅助检查'.\n"
                    "不要把“诊疗经过”误当成“诊治经过”；先按章节和业务用途判断。不要把“辅助检查”绑定到“病历摘要”。如果reason说明候选不包含、不匹配或不能表达目标含义，必须省略该映射。\n"
                    "Do not merge candidates. Select the single best section-range node when it represents the complete section; otherwise select the exact leaf node.\n"
                    "If no candidate has the required meaning and section context, omit that standard_node_id from mappings.\n"
                    "Confidence must be a number from 0 to 1. Keep reason short and factual."
                ),
            },
            {
                "role": "user",
                "content": (
                    "严格返回以下格式，不要增加任何字段：{\"mappings\":[{\"standard_node_id\":\"...\",\"html_node_key\":\"...\",\"confidence\":0.0,\"reason\":\"...\"}]}；没有匹配时返回{\"mappings\":[]}。\n"
                    + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                ),
            },
        ]

    @staticmethod
    def _semantic_messages(payload: list[dict[str, Any]]) -> list[dict[str, str]]:
        return [
            {
                "role": "system",
                "content": (
                    "你是临床模板节点匹配器。每个标准节点只能从其候选HTML节点中选择；"
                    "无法确定就不返回。不得生成候选集合外的任何ID。"
                    "node_value和description是标准节点的业务用途说明，不是待匹配的实际病历值。"
                    "应综合标准节点有效名称、父级路径、用途说明、节点角色，以及HTML候选的字段名、"
                    "占位符、局部标签、所属章节、锚点路径和上下文判断。"
                    "临床文书中的规范名称与模板常用名称可能存在同义词、简称、词序变化、"
                    "字段后缀差异或值表示精度差异；遇到明确对应且章节语义一致的候选，应优先选择该候选。"
                    "不要只按字面重合判断，也不要因为存在一个字面相似候选就强行绑定。"
                    "text、value、content等通用叶子名本身没有业务含义，必须结合其父级路径判断；"
                    "输入中的standard_name_forms和name_forms只是通用归一化提示，最终仍需结合业务语义核对。"
                    "只返回一个合法JSON对象，不要Markdown、解释或额外文字。"
                ),
            },
            {
                "role": "user",
                "content": (
                    "返回 {\"mappings\": [...]}。每项包含 standard_node_id、html_node_key、"
                    "confidence(0到1)、reason；reason不超过20个字。"
                    "每个standard_node_id最多返回一项。\n"
                    + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                ),
            },
        ]

    @staticmethod
    def _semantic_empty_retry_messages(payload: list[dict[str, Any]]) -> list[dict[str, str]]:
        return [
            {
                "role": "system",
                "content": (
                    "你是临床模板节点匹配器。请逐个判断输入的标准节点。"
                    "如果候选中存在明确的临床同义、简称、词序变体或字段表示变体，必须选择最合适的候选；"
                    "只有候选确实无法表达标准节点含义时才返回空结果。"
                    "必须结合父级路径、章节、节点用途和候选上下文判断，不能只看字段字面。"
                    "只能选择输入候选中的standard_node_id和html_node_key。"
                    "只返回一个合法JSON对象，不要Markdown、解释或额外文字。"
                ),
            },
            {
                "role": "user",
                "content": (
                    "返回 {\"mappings\": [...]}。每项包含 standard_node_id、html_node_key、"
                    "confidence(0到1)、reason；reason不超过20个字。"
                    "每个standard_node_id最多返回一项。\n"
                    + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                ),
            },
        ]

    @classmethod
    def _semantic_retry_messages(cls, payload: list[dict[str, Any]]) -> list[dict[str, str]]:
        messages = cls._semantic_messages(payload)
        messages[0] = {
            "role": "system",
            "content": (
                "严格执行JSON输出。只输出一个可被Python json.loads解析的JSON对象，"
                "格式必须是 {\"mappings\":[]}。所有键和值使用双引号；"
                "每个字段之间必须有逗号；禁止尾逗号、Markdown代码块、换行说明和思考过程。"
                "只能选择输入候选中的standard_node_id和html_node_key。"
            ),
        }
        return messages

    @staticmethod
    def _parse_semantic_response(
        response: str,
        allowed: dict[str, set[str]],
    ) -> dict[str, dict[str, Any]]:
        parsed = _json_object(response)
        rows = parsed.get("mappings") or []
        if not isinstance(rows, list):
            raise ValueError("LLM 节点匹配结果 mappings 必须是数组")
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            standard_id = str(row.get("standard_node_id") or "")
            html_key = str(row.get("html_node_key") or "")
            if standard_id not in allowed or html_key not in allowed[standard_id]:
                continue
            try:
                confidence = min(1.0, max(0.0, float(row.get("confidence", 0))))
            except (TypeError, ValueError):
                confidence = 0.0
            reason = str(row.get("reason") or "")
            if NodeMatcher._semantic_reason_rejects_selection(reason):
                continue
            result[standard_id] = {
                "html_node_key": html_key,
                "confidence": confidence,
                "reason": reason,
            }
        return result

    @staticmethod
    def _semantic_reason_rejects_selection(reason: str) -> bool:
        """Reject model rows whose own explanation says the candidate is invalid."""
        normalized = normalize_text(reason)
        if not normalized:
            return False
        negative_markers = (
            "不包含",
            "不匹配",
            "不对应",
            "不能表达",
            "无法表达",
            "无关",
            "notmatch",
            "doesnotmatch",
            "unrelated",
            "cannotrepresent",
        )
        return any(marker in normalized for marker in negative_markers)

    @staticmethod
    def _truncate(value: object, limit: int) -> str:
        text = str(value or "").strip()
        if len(text) <= limit:
            return text
        return text[: max(1, limit - 3)] + "..."

    @staticmethod
    def _standard_semantic_labels(node: dict[str, Any]) -> tuple[str, ...]:
        labels: list[str] = []
        for value in (node.get("node_cn"), node.get("node_en")):
            text = str(value or "").strip()
            if text and normalize_text(text) not in _GENERIC_VALUE_NAMES:
                labels.append(text)
        path_labels = tuple(split_path(node.get("path_text")))
        for value in reversed(path_labels):
            text = str(value or "").strip()
            if text and normalize_text(text) not in _GENERIC_VALUE_NAMES:
                labels.append(text)
                break
        if not labels:
            fallback = str(node.get("node_cn") or node.get("node_en") or node.get("id") or "").strip()
            if fallback:
                labels.append(fallback)
        return tuple(dict.fromkeys(labels))

    @classmethod
    def _standard_name(cls, node: dict[str, Any]) -> str:
        labels = cls._standard_semantic_labels(node)
        return labels[0] if labels else str(node.get("id") or "")

    @staticmethod
    def _html_name(node: dict[str, Any]) -> str:
        return str(node.get("display_text") or node.get("placeholder") or node.get("anchor_name") or node.get("node_key") or "")

    @staticmethod
    def _standard_order(node: dict[str, Any]) -> tuple[int, str]:
        try:
            order = int(node.get("order") or 0)
        except (TypeError, ValueError):
            order = 0
        return order, str(node.get("id") or "")
