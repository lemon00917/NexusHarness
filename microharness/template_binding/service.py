"""Read-only orchestration for automatic template and node recommendations."""

from __future__ import annotations

import os
import time
from typing import Any, Callable

from .html_parser import HtmlTemplateDecodeError, parse_html_info
from .node_matcher import NodeMatcher
from .standard_tree import StandardNodeTreeError, build_standard_tree
from .template_matcher import ChatClient, TemplateMatcher
from .validator import BindingRecommendationValidator


class TemplateBindingAnalysisError(RuntimeError):
    """Raised when the requested source data cannot be analyzed."""


class TemplateBindingAnalysisService:
    def __init__(
        self,
        repository: Any,
        *,
        template_matcher: TemplateMatcher | None = None,
        node_matcher: NodeMatcher | None = None,
        validator: BindingRecommendationValidator | None = None,
        llm_factory: Callable[[str], ChatClient] | None = None,
    ) -> None:
        self.repository = repository
        self.template_matcher = template_matcher or TemplateMatcher()
        self.node_matcher = node_matcher or NodeMatcher()
        self.validator = validator or BindingRecommendationValidator()
        self.llm_factory = llm_factory or self._default_llm_factory
        # The analysis service is request-scoped in the API and is reused by
        # the read-only batch evaluator.  Cache only within this instance so
        # one batch does not repeat the global standard-template queries while
        # separate requests still observe current database metadata.
        self._standard_templates_cache: list[dict[str, Any]] | None = None
        self._standard_template_cache: dict[str, dict[str, Any] | None] = {}
        self._standard_nodes_cache: dict[str, list[dict[str, Any]]] = {}
        self._cache_stats = {
            "standard_template_catalog_queries": 0,
            "standard_template_catalog_cache_hits": 0,
            "standard_template_queries": 0,
            "standard_template_cache_hits": 0,
            "standard_node_queries": 0,
            "standard_node_cache_hits": 0,
        }

    def analyze(
        self,
        *,
        html_template_id: str,
        html_category_id: str,
        standard_template_id: str | None = None,
        template_match_model: str | None = None,
        node_match_model: str | None = None,
        use_llm: bool = True,
        existing_mapping_policy: str = "reference",
    ) -> dict[str, Any]:
        started = time.perf_counter()
        stage_times: dict[str, int] = {}

        def mark_stage(name: str, stage_started: float) -> None:
            stage_times[name] = round((time.perf_counter() - stage_started) * 1000)

        warnings: list[str] = []
        existing_mapping_policy = self._normalize_existing_mapping_policy(
            existing_mapping_policy, warnings
        )
        llm_models = self._llm_model_summary(
            use_llm=use_llm,
            template_match_model=template_match_model,
            node_match_model=node_match_model,
        )
        stage_started = time.perf_counter()
        html_template = self.repository.get_html_template(
            html_template_id, html_category_id, include_html=True
        )
        mark_stage("html_template_query_ms", stage_started)
        if html_template is None:
            raise TemplateBindingAnalysisError("HTML template not found for the composite key")
        html_info = html_template.pop("html_info", None)
        if not html_info:
            raise TemplateBindingAnalysisError("HTML template has no html_info")
        try:
            stage_started = time.perf_counter()
            parsed_html = parse_html_info(html_info)
            mark_stage("html_parse_ms", stage_started)
        except HtmlTemplateDecodeError as exc:
            raise TemplateBindingAnalysisError(str(exc)) from exc

        stage_started = time.perf_counter()
        identity_check = self._check_html_identity(
            html_template_id, html_category_id, parsed_html["html_sha256"]
        )
        mark_stage("html_identity_ms", stage_started)
        warnings.extend(identity_check["warnings"])
        if identity_check["status"] == "CONFLICT":
            return self._blocked_result(
                started,
                html_template,
                parsed_html,
                identity_check,
                existing_mapping_policy,
                llm_models,
                warnings,
            )

        stage_started = time.perf_counter()
        standard_templates = self._all_standard_templates()
        mark_stage("standard_catalog_ms", stage_started)
        stage_started = time.perf_counter()
        existing = self.repository.get_existing_mappings(html_template_id)
        mark_stage("existing_mappings_ms", stage_started)
        stage_started = time.perf_counter()
        template_match = self.template_matcher.match(
            html_template=html_template,
            html_nodes=parsed_html["nodes"],
            standard_templates=standard_templates,
            existing_template_mappings=existing.get("template_mappings") or [],
            requested_standard_template_id=standard_template_id,
            existing_mapping_policy=existing_mapping_policy,
            llm_client=self._llm_client(use_llm, template_match_model, warnings, "模板"),
        )
        mark_stage("template_match_ms", stage_started)
        warnings.extend(template_match.get("warnings") or [])
        selected_id = template_match.get("selected_template_id")
        if not selected_id:
            return self._result_without_nodes(
                started,
                html_template,
                parsed_html,
                identity_check,
                existing,
                template_match,
                existing_mapping_policy,
                llm_models,
                warnings,
            )

        stage_started = time.perf_counter()
        selected = self._get_standard_template(str(selected_id))
        mark_stage("selected_template_ms", stage_started)
        if selected is None:
            template_match["status"] = "CONFLICT"
            warnings.append("推荐模板不存在或不属于临床文档类型(type=3)")
            return self._result_without_nodes(
                started,
                html_template,
                parsed_html,
                identity_check,
                existing,
                template_match,
                existing_mapping_policy,
                llm_models,
                warnings,
            )

        stage_started = time.perf_counter()
        rows = self._list_standard_nodes(str(selected_id))
        try:
            standard_tree = build_standard_tree(rows, str(selected_id))
            mark_stage("standard_nodes_and_tree_ms", stage_started)
        except StandardNodeTreeError as exc:
            raise TemplateBindingAnalysisError(str(exc)) from exc
        if standard_tree["node_count"] == 0:
            template_match["status"] = "CONFLICT"
            warnings.append(
                "The selected standard template has no standard nodes; node binding was not considered successful."
            )

        selected_node_mappings = [
            row
            for row in (existing.get("node_mappings") or [])
            if str(row.get("standard_template_id") or "") == str(selected_id)
        ]
        stage_started = time.perf_counter()
        node_match = self.node_matcher.match(
            standard_nodes=standard_tree["flat_nodes"],
            html_nodes=parsed_html["nodes"],
            existing_node_mappings=selected_node_mappings,
            existing_mapping_policy=existing_mapping_policy,
            llm_client=self._llm_client(use_llm, node_match_model, warnings, "节点"),
        )
        mark_stage("node_match_ms", stage_started)
        warnings.extend(node_match.get("warnings") or [])
        stage_started = time.perf_counter()
        validation = self.validator.validate(
            selected_template=selected,
            standard_nodes=standard_tree["flat_nodes"],
            html_nodes=parsed_html["nodes"],
            node_match=node_match,
        )
        mark_stage("validation_ms", stage_started)
        warnings.extend(validation.get("warnings") or [])
        node_match["mappings"] = validation["mappings"]
        node_match["mapping_count"] = validation["mapping_count"]

        return {
            "read_only": True,
            "status": self._overall_status(identity_check, template_match, node_match, validation),
            "html": self._html_result(html_template, parsed_html),
            "identity_check": identity_check,
            "existing_mappings": existing,
            "existing_mapping_policy": existing_mapping_policy,
            "llm_models": llm_models,
            "template_match": template_match,
            "standard": {
                "template": selected,
                "node_count": standard_tree["node_count"],
                "bindable_count": standard_tree["bindable_count"],
                "container_count": standard_tree["container_count"],
                "root_count": standard_tree["root_count"],
            },
            "node_match": node_match,
            "validation": validation,
            "performance": {
                "stages_ms": stage_times,
                "template_catalog_cache": dict(self._cache_stats),
            },
            "warnings": list(dict.fromkeys(warnings)),
            "elapsed_ms": round((time.perf_counter() - started) * 1000),
        }

    def _all_standard_templates(self) -> list[dict[str, Any]]:
        if self._standard_templates_cache is not None:
            self._cache_stats["standard_template_catalog_cache_hits"] += 1
            return [dict(item) for item in self._standard_templates_cache]

        self._cache_stats["standard_template_catalog_queries"] += 1
        page = 1
        items: list[dict[str, Any]] = []
        while True:
            result = self.repository.list_standard_templates(page=page, page_size=200)
            batch = [
                dict(item)
                for item in (result.get("items") or [])
                if self._is_active_standard_template(item)
            ]
            items.extend(batch)
            if len(items) >= int(result.get("total") or 0) or not batch:
                self._standard_templates_cache = [dict(item) for item in items]
                return [dict(item) for item in self._standard_templates_cache]
            page += 1

    @staticmethod
    def _is_active_standard_template(template: dict[str, Any]) -> bool:
        """Only status=1 standard clinical document templates are bindable."""
        return str(template.get("status") or "").strip() == "1"

    def _get_standard_template(self, template_id: str) -> dict[str, Any] | None:
        key = str(template_id)
        if key in self._standard_template_cache:
            self._cache_stats["standard_template_cache_hits"] += 1
            cached = self._standard_template_cache[key]
            return dict(cached) if cached is not None else None

        # The catalog already contains the complete type-3 standard-template
        # population and its node count. Reuse that row instead of issuing a
        # second query whose aggregate node-count subquery scans the node table
        # again. A direct lookup remains the fallback for an ID outside the
        # loaded catalog or for repositories without catalog data.
        if self._standard_templates_cache is not None:
            for item in self._standard_templates_cache:
                if str(item.get("id") or "") == key:
                    selected = dict(item)
                    selected.setdefault("category_type", "3")
                    self._standard_template_cache[key] = selected
                    self._cache_stats["standard_template_cache_hits"] += 1
                    return dict(selected)

        self._cache_stats["standard_template_queries"] += 1
        selected = self.repository.get_standard_template(key)
        if selected is not None and not self._is_active_standard_template(selected):
            selected = None
        self._standard_template_cache[key] = dict(selected) if selected is not None else None
        return dict(selected) if selected is not None else None

    def _list_standard_nodes(self, template_id: str) -> list[dict[str, Any]]:
        key = str(template_id)
        if key in self._standard_nodes_cache:
            self._cache_stats["standard_node_cache_hits"] += 1
            return [dict(row) for row in self._standard_nodes_cache[key]]

        self._cache_stats["standard_node_queries"] += 1
        rows = self.repository.list_standard_nodes(key)
        self._standard_nodes_cache[key] = [dict(row) for row in rows]
        return [dict(row) for row in self._standard_nodes_cache[key]]

    def _check_html_identity(
        self, template_id: str, category_id: str, selected_hash: str
    ) -> dict[str, Any]:
        rows = self.repository.list_html_template_variants(template_id)
        hashes: dict[str, list[str]] = {}
        failures: list[str] = []
        variants: list[dict[str, Any]] = []
        for row in rows:
            variant_category = str(row.get("print_template_category_id") or "")
            html_info = row.get("html_info")
            try:
                digest = parse_html_info(html_info)["html_sha256"] if html_info else ""
            except HtmlTemplateDecodeError:
                digest = ""
                failures.append(variant_category)
            if variant_category == str(category_id) and digest and digest != selected_hash:
                failures.append(variant_category)
            if digest:
                hashes.setdefault(digest, []).append(variant_category)
            variants.append(
                {
                    "category_id": variant_category,
                    "category_name": str(row.get("category_name") or ""),
                    "html_name": str(row.get("html_name") or ""),
                    "html_sha256": digest,
                }
            )
        warnings: list[str] = []
        if len(hashes) > 1:
            warnings.append("同一HTML模板ID在不同分类下对应不同内容，禁止自动推荐绑定")
            status = "CONFLICT"
        elif failures and len(rows) > 1:
            warnings.append("同一HTML模板ID存在无法解码的分类变体，需人工核验后再绑定")
            status = "REVIEW_REQUIRED"
        else:
            status = "VERIFIED"
        return {
            "status": status,
            "template_id": str(template_id),
            "selected_category_id": str(category_id),
            "variant_count": len(rows),
            "content_hash_count": len(hashes),
            "variants": variants,
            "warnings": warnings,
        }

    @staticmethod
    def _normalize_existing_mapping_policy(policy: str, warnings: list[str]) -> str:
        """Keep unknown policies from accidentally changing the default trust model."""
        normalized = str(policy or "reference").strip().lower()
        if normalized in {"reference", "authoritative"}:
            return normalized
        warnings.append(
            f"未知的已有绑定策略 {policy!r}，已降级为 reference；已有绑定不会作为自动匹配答案"
        )
        return "reference"

    @classmethod
    def _llm_model_summary(
        cls,
        *,
        use_llm: bool,
        template_match_model: str | None,
        node_match_model: str | None,
    ) -> dict[str, Any]:
        if not use_llm:
            return {"enabled": False, "template_match_model": None, "node_match_model": None}
        return {
            "enabled": True,
            "template_match_model": cls._resolve_model_name(template_match_model),
            "node_match_model": cls._resolve_model_name(node_match_model),
        }

    @staticmethod
    def _resolve_model_name(model: str | None) -> str:
        return str(model or os.getenv("TEMPLATE_BINDING_MODEL") or "qwen2.5:3b").strip()

    def _llm_client(
        self, use_llm: bool, model: str | None, warnings: list[str], label: str
    ) -> ChatClient | None:
        if not use_llm:
            return None
        model_name = self._resolve_model_name(model)
        try:
            return self.llm_factory(model_name)
        except Exception as exc:
            warnings.append(f"{label}LLM不可用，已降级为规则推荐：{exc}")
            return None

    @staticmethod
    def _default_llm_factory(model: str) -> ChatClient:
        from microharness.ollama import OllamaClient

        # Node matching returns structured mappings with node metadata and
        # candidate context. Keep enough context/output budget for a batch;
        # node_matcher still falls back to one-node retries if a small model
        # produces malformed JSON.
        return OllamaClient(
            model=model,
            timeout=60,
            format_json=True,
            num_ctx=8192,
            num_predict=2048,
        )

    @staticmethod
    def _html_result(template: dict[str, Any], parsed: dict[str, Any]) -> dict[str, Any]:
        return {
            "template": template,
            "html_sha256": parsed["html_sha256"],
            "html_length": parsed["html_length"],
            "node_count": parsed["node_count"],
            "nodes": parsed["nodes"],
            "warnings": parsed.get("warnings") or [],
        }

    def _blocked_result(
        self,
        started: float,
        html_template: dict[str, Any],
        parsed_html: dict[str, Any],
        identity_check: dict[str, Any],
        existing_mapping_policy: str,
        llm_models: dict[str, Any],
        warnings: list[str],
    ) -> dict[str, Any]:
        return {
            "read_only": True,
            "status": "CONFLICT",
            "html": self._html_result(html_template, parsed_html),
            "identity_check": identity_check,
            "existing_mappings": {},
            "existing_mapping_policy": existing_mapping_policy,
            "llm_models": llm_models,
            "template_match": {"status": "BLOCKED", "selected_template_id": None, "candidates": []},
            "standard": None,
            "node_match": None,
            "validation": {"status": "NOT_RUN", "valid": False},
            "performance": {"template_catalog_cache": dict(self._cache_stats)},
            "warnings": list(dict.fromkeys(warnings)),
            "elapsed_ms": round((time.perf_counter() - started) * 1000),
        }

    def _result_without_nodes(
        self,
        started: float,
        html_template: dict[str, Any],
        parsed_html: dict[str, Any],
        identity_check: dict[str, Any],
        existing: dict[str, Any],
        template_match: dict[str, Any],
        existing_mapping_policy: str,
        llm_models: dict[str, Any],
        warnings: list[str],
    ) -> dict[str, Any]:
        status = "CONFLICT" if template_match.get("status") == "CONFLICT" else "REVIEW_REQUIRED"
        return {
            "read_only": True,
            "status": status,
            "html": self._html_result(html_template, parsed_html),
            "identity_check": identity_check,
            "existing_mappings": existing,
            "existing_mapping_policy": existing_mapping_policy,
            "llm_models": llm_models,
            "template_match": template_match,
            "standard": None,
            "node_match": None,
            "validation": {"status": "NOT_RUN", "valid": False},
            "performance": {"template_catalog_cache": dict(self._cache_stats)},
            "warnings": list(dict.fromkeys(warnings)),
            "elapsed_ms": round((time.perf_counter() - started) * 1000),
        }

    @staticmethod
    def _overall_status(
        identity: dict[str, Any],
        template_match: dict[str, Any],
        node_match: dict[str, Any],
        validation: dict[str, Any],
    ) -> str:
        if identity.get("status") == "CONFLICT" or validation.get("status") == "CONFLICT":
            return "CONFLICT"
        if (
            identity.get("status") == "REVIEW_REQUIRED"
            or template_match.get("status") == "REVIEW_REQUIRED"
            or node_match.get("status") == "REVIEW_REQUIRED"
        ):
            return "REVIEW_REQUIRED"
        return "COMPLETED"
