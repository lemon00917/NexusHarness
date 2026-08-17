"""Build and validate standard clinical-document node trees."""

from __future__ import annotations

from collections import defaultdict
from typing import Any


class StandardNodeTreeError(ValueError):
    """Raised when standard nodes do not form a valid template-owned tree."""


def normalize_standard_node(row: dict[str, Any], template_id: str) -> dict[str, Any]:
    node_id = str(row.get("id") or "")
    if not node_id:
        raise StandardNodeTreeError("standard node has no id")
    owner = str(row.get("standard_xml_id") or template_id)
    parent_value = row.get("pid_new") or row.get("pid")
    parent_id = str(parent_value) if parent_value not in (None, "") else None
    node_cn = str(row.get("node_cn") or "")
    node_en = str(row.get("node_en") or "")
    description_parts = [row.get("node_remark"), row.get("node_value"), row.get("mapping_value")]
    description = " | ".join(str(item) for item in description_parts if item not in (None, ""))
    return {
        "id": node_id,
        "template_id": owner,
        "parent_id": parent_id,
        "path_ids": [],
        "path_text": "",
        "node_en": node_en,
        "node_cn": node_cn,
        "node_attr": str(row.get("node_attr") or ""),
        "node_value": str(row.get("node_value") or ""),
        "mapping_value": str(row.get("mapping_value") or ""),
        "description": description,
        # The final bindable decision is made after the parent/child graph is
        # known in ``build_standard_tree``.  Every named node is not
        # necessarily a value-bearing node; many are structural containers.
        "bindable": False,
        "node_role": "unknown",
        "child_count": 0,
        "bindable_reason": "",
        "order": row.get("seq_no") if row.get("seq_no") is not None else 0,
        "show_status": str(row.get("show_status") or ""),
    }


def build_standard_tree(rows: list[dict[str, Any]], template_id: str) -> dict[str, Any]:
    expected = str(template_id)
    nodes = [normalize_standard_node(row, expected) for row in rows]
    by_id: dict[str, dict[str, Any]] = {}
    for node in nodes:
        if node["template_id"] != expected:
            raise StandardNodeTreeError(
                f"node {node['id']} belongs to template {node['template_id']}, expected {expected}"
            )
        if node["id"] in by_id:
            raise StandardNodeTreeError(f"duplicate standard node id: {node['id']}")
        by_id[node["id"]] = node

    children: dict[str | None, list[dict[str, Any]]] = defaultdict(list)
    for node in nodes:
        parent_id = node["parent_id"]
        # Some DMP datasets use 0/-1 as a root sentinel instead of NULL.
        if parent_id in {"0", "-1"} and parent_id not in by_id:
            parent_id = None
            node["parent_id"] = None
        if parent_id is not None and parent_id not in by_id:
            raise StandardNodeTreeError(f"orphan standard node {node['id']} -> {parent_id}")
        children[parent_id].append(node)

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: dict[str, Any], path_ids: list[str], path_names: list[str]) -> None:
        node_id = node["id"]
        if node_id in visiting:
            raise StandardNodeTreeError(f"cycle detected at standard node {node_id}")
        if node_id in visited:
            return
        visiting.add(node_id)
        label = node["node_cn"] or node["node_en"] or node_id
        node["path_ids"] = [*path_ids, node_id]
        node["path_text"] = "/".join([*path_names, label])
        for child in sorted(children.get(node_id, []), key=_sort_key):
            visit(child, node["path_ids"], [*path_names, label])
        visiting.remove(node_id)
        visited.add(node_id)

    roots = sorted(children.get(None, []), key=_sort_key)
    for root in roots:
        visit(root, [], [])
    if len(visited) != len(nodes):
        raise StandardNodeTreeError("standard node graph contains an unreachable cycle")

    # Infer node roles from generic tree structure and available metadata.
    # This deliberately does not depend on document names or field names.
    # Leaves are normally bindable value nodes.  A container is bindable only
    # when its own row carries value/mapping metadata.
    for node in nodes:
        child_count = len(children.get(node["id"], []))
        is_named = bool(
            str(node.get("node_cn") or "").strip()
            or str(node.get("node_en") or "").strip()
        )
        has_value_metadata = any(
            str(node.get(key) or "").strip()
            for key in ("node_value", "mapping_value", "node_attr")
        )
        if child_count == 0:
            bindable = is_named
            role = "value" if bindable else "unnamed_leaf"
            reason = "leaf_with_label" if bindable else "leaf_without_label"
        elif is_named and has_value_metadata:
            bindable = True
            role = "value_container"
            reason = "container_with_value_metadata"
        else:
            bindable = False
            role = "container"
            reason = "structural_container"
        node["child_count"] = child_count
        node["node_role"] = role
        node["bindable"] = bindable
        node["bindable_reason"] = reason

    def materialize(node: dict[str, Any]) -> dict[str, Any]:
        result = dict(node)
        result["children"] = [materialize(child) for child in sorted(children.get(node["id"], []), key=_sort_key)]
        return result

    return {
        "template_id": expected,
        "node_count": len(nodes),
        "bindable_count": sum(1 for node in nodes if node.get("bindable")),
        "container_count": sum(1 for node in nodes if node.get("node_role") == "container"),
        "root_count": len(roots),
        "roots": [materialize(root) for root in roots],
        "flat_nodes": nodes,
    }


def _sort_key(node: dict[str, Any]) -> tuple[int, str]:
    try:
        order = int(node.get("order") or 0)
    except (TypeError, ValueError):
        order = 0
    return order, str(node.get("id") or "")
