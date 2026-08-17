"""Strict ownership and identity validation for binding recommendations."""

from __future__ import annotations

from typing import Any


class BindingRecommendationValidator:
    """Validate recommendations against server-owned template and node sets."""

    def validate(
        self,
        *,
        selected_template: dict[str, Any],
        standard_nodes: list[dict[str, Any]],
        html_nodes: list[dict[str, Any]],
        node_match: dict[str, Any],
    ) -> dict[str, Any]:
        errors: list[str] = []
        warnings: list[str] = []
        template_id = str(selected_template.get("id") or "")
        if not template_id:
            errors.append("推荐结果没有标准模板ID")
        if str(selected_template.get("category_type") or "") != "3":
            errors.append("推荐的标准模板不是临床文档类型(type=3)")

        standard_by_id: dict[str, dict[str, Any]] = {}
        for node in standard_nodes:
            node_id = str(node.get("id") or "")
            owner = str(node.get("template_id") or node.get("standard_xml_id") or "")
            if not node_id:
                errors.append("标准节点缺少ID")
                continue
            if owner != template_id:
                errors.append(f"标准节点 {node_id} 不属于模板 {template_id}")
                continue
            if node_id in standard_by_id:
                errors.append(f"标准节点ID重复: {node_id}")
                continue
            standard_by_id[node_id] = node

        html_by_key = {
            str(node.get("node_key")): node
            for node in html_nodes
            if node.get("node_key") not in (None, "")
        }
        seen_standard: set[str] = set()
        seen_html: set[str] = set()
        valid: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []

        for recommendation in node_match.get("mappings") or []:
            item = dict(recommendation)
            reasons: list[str] = []
            standard_id = str(item.get("standard_node_id") or "")
            html_keys = [str(value) for value in (item.get("html_node_keys") or []) if value]
            if standard_id not in standard_by_id:
                reasons.append("标准节点不属于当前标准模板")
            if standard_id in seen_standard:
                reasons.append("同一标准节点出现重复推荐")
            unknown_html = [key for key in html_keys if key not in html_by_key]
            if unknown_html:
                reasons.append("包含不属于当前HTML模板的节点")
            if not html_keys:
                reasons.append("未提供HTML节点")
            reused = [key for key in html_keys if key in seen_html]
            authoritative_existing = (
                str(item.get("source") or "").strip().lower() == "existing"
                and str(item.get("status") or "").strip().upper() == "EXISTING"
            )
            if reused and not authoritative_existing:
                reasons.append("HTML节点已被其他标准节点占用")

            if reasons:
                item["validation_errors"] = reasons
                rejected.append(item)
                errors.append(f"节点推荐 {standard_id or '<empty>'} 被拒绝：{'；'.join(reasons)}")
                continue

            seen_standard.add(standard_id)
            seen_html.update(html_keys)
            valid.append(item)

        if rejected:
            warnings.append(f"已拒绝 {len(rejected)} 条不满足所有权或唯一性约束的节点推荐")
        return {
            "status": "VALID" if not errors else "CONFLICT",
            "valid": not errors,
            "mappings": valid,
            "mapping_count": len(valid),
            "rejected": rejected,
            "errors": errors,
            "warnings": warnings,
        }
