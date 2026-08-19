"""Validated Stage4 persistence for reviewed template-binding recommendations."""

from __future__ import annotations

import re
import uuid
from typing import Any

from .html_parser import HtmlTemplateDecodeError, parse_html_info
from .id_provider import IdProvider, get_id_provider
from .standard_tree import StandardNodeTreeError, build_standard_tree


class TemplateBindingCommitError(RuntimeError):
    """Raised when a reviewed binding payload fails server-side validation."""


_CODE_SELECTOR_RE = re.compile(r"^code:(?P<code>[^\s;]+)$", re.IGNORECASE)
_SCODE_SELECTOR_RE = re.compile(r"^scode:(?P<code>[^\s;]+)$", re.IGNORECASE)


class TemplateBindingCommitService:
    def __init__(self, repository: Any, *, id_provider: IdProvider | None = None) -> None:
        self.repository = repository
        self.id_provider = id_provider or get_id_provider()

    def commit(
        self,
        *,
        html_template_id: str,
        html_category_id: str,
        standard_template_id: str,
        node_mappings: list[dict[str, Any]],
        expected_update_time: str | None = None,
    ) -> dict[str, Any]:
        html_template = self.repository.get_html_template(
            html_template_id, html_category_id, include_html=True
        )
        if html_template is None:
            raise TemplateBindingCommitError("HTML template not found for the composite key")
        html_info = html_template.get("html_info")
        if not html_info:
            raise TemplateBindingCommitError("HTML template has no html_info")
        try:
            parsed_html = parse_html_info(html_info)
        except HtmlTemplateDecodeError as exc:
            raise TemplateBindingCommitError(str(exc)) from exc

        standard_template = self.repository.get_standard_template(standard_template_id)
        if standard_template is None or str(standard_template.get("status") or "").strip() != "1":
            raise TemplateBindingCommitError(
                "standard template not found, inactive, or is not a clinical document template"
            )
        try:
            standard_tree = build_standard_tree(
                self.repository.list_standard_nodes(standard_template_id), standard_template_id
            )
        except StandardNodeTreeError as exc:
            raise TemplateBindingCommitError(str(exc)) from exc

        existing = self.repository.get_existing_mappings(html_template_id)
        template_rows = existing.get("template_mappings") or []
        conflicting = [
            row
            for row in template_rows
            if str(row.get("standard_xml_id") or "") != str(standard_template_id)
            and self._existing_mapping_is_active_or_unknown(row)
        ]
        if conflicting:
            raise TemplateBindingCommitError(
                "HTML template is already bound to a different standard template"
            )

        normalized = self._validate_and_normalize_nodes(
            submitted=node_mappings,
            html_nodes=parsed_html["nodes"],
            standard_nodes=standard_tree["flat_nodes"],
            standard_template_id=standard_template_id,
        )
        result = self.repository.save_reviewed_bindings(
            mapping_id=uuid.uuid4().hex,
            html_template_id=html_template_id,
            html_category_id=html_category_id,
            standard_template_id=standard_template_id,
            expected_update_time=expected_update_time,
            node_mappings=normalized,
            id_provider=self.id_provider,
        )
        return {
            "saved": True,
            "mode": "PATCH",
            "html_template_id": html_template_id,
            "html_category_id": html_category_id,
            "standard_template_id": standard_template_id,
            **result,
        }

    @staticmethod
    def _existing_mapping_is_active_or_unknown(row: dict[str, Any]) -> bool:
        """Keep blocking valid/legacy bindings; inactive bindings can be replaced."""
        status = row.get("standard_template_status")
        if status is None or str(status).strip() == "":
            return True
        return str(status).strip() == "1"

    @staticmethod
    def _validate_and_normalize_nodes(
        *,
        submitted: list[dict[str, Any]],
        html_nodes: list[dict[str, Any]],
        standard_nodes: list[dict[str, Any]],
        standard_template_id: str,
    ) -> list[dict[str, Any]]:
        html_by_key = {
            str(node.get("node_key")): node
            for node in html_nodes
            if node.get("node_key") not in (None, "")
        }
        standard_by_id = {
            str(node.get("id")): node
            for node in standard_nodes
            if str(node.get("standard_xml_id") or node.get("template_id") or "")
            == str(standard_template_id)
        }
        seen_standard: set[str] = set()
        seen_html: set[str] = set()
        normalized: list[dict[str, Any]] = []

        for index, item in enumerate(submitted, start=1):
            standard_id = str(item.get("standard_node_id") or "").strip()
            html_keys = list(
                dict.fromkeys(
                    str(value).strip()
                    for value in (item.get("html_node_keys") or [])
                    if str(value).strip()
                )
            )
            if standard_id not in standard_by_id:
                raise TemplateBindingCommitError(
                    f"node mapping {index} contains an unknown or cross-template standard node"
                )
            if standard_id in seen_standard:
                raise TemplateBindingCommitError(
                    f"standard node {standard_id} is submitted more than once"
                )
            if not html_keys:
                raise TemplateBindingCommitError(f"node mapping {index} has no HTML nodes")
            unknown_html = [key for key in html_keys if key not in html_by_key]
            if unknown_html:
                raise TemplateBindingCommitError(
                    f"node mapping {index} contains unknown HTML nodes: {', '.join(unknown_html)}"
                )
            reused_html = [key for key in html_keys if key in seen_html]
            if reused_html:
                raise TemplateBindingCommitError(
                    "HTML nodes cannot be assigned to multiple standard nodes: "
                    + ", ".join(reused_html)
                )

            selected_html = [html_by_key[key] for key in html_keys]
            if TemplateBindingCommitService._is_section_text_node(standard_by_id[standard_id]):
                section_name = TemplateBindingCommitService._section_text_name(standard_by_id[standard_id])
                scoped_html = [
                    node
                    for node in selected_html
                    if node.get("scope_selectors") and node.get("scope_mapping_value")
                ]
                if scoped_html and not any(
                    TemplateBindingCommitService._section_name_matches(section_name, node)
                    for node in scoped_html
                ):
                    raise TemplateBindingCommitError(
                        f"HTML nodes for section {section_name} do not match the selected clinical section"
                    )
            section_scope = TemplateBindingCommitService._section_scope_mapping(
                standard_by_id[standard_id], selected_html, html_nodes
            )
            if section_scope is not None:
                normalized.append(
                    {
                        "standard_node_id": standard_id,
                        "html_node_id": ";".join(section_scope["selectors"]),
                        "html_node_code": ";".join(section_scope["selectors"]),
                        "mapping_values": ";".join(section_scope["mapping_values"]),
                    }
                )
                seen_standard.add(standard_id)
                seen_html.update(html_keys)
                continue

            selected_html = TemplateBindingCommitService._expand_mapping_sources(
                selected_html, html_nodes
            )
            mapping_pairs: list[tuple[str, str]] = []
            discovered_codes: list[str] = []
            discovered_values: list[str] = []
            for node in selected_html:
                node_codes = TemplateBindingCommitService._node_code_selectors(node)
                node_values = TemplateBindingCommitService._mapping_value_tokens(node)
                discovered_codes.extend(node_codes)
                discovered_values.extend(node_values)
                if not node_codes or not node_values:
                    continue
                if len(node_codes) == len(node_values):
                    mapping_pairs.extend(zip(node_codes, node_values))
                else:
                    mapping_pairs.extend((node_codes[0], value) for value in node_values)
            mapping_pairs = list(dict.fromkeys(mapping_pairs))
            if not discovered_codes:
                raise TemplateBindingCommitError(
                    f"HTML node {html_keys[0]} has no canonical code selector for html_node_id"
                )
            if not discovered_values:
                raise TemplateBindingCommitError(
                    f"HTML node {html_keys[0]} has no content or placeholder for mapping_values"
                )
            if not mapping_pairs:
                raise TemplateBindingCommitError(
                    f"HTML node {html_keys[0]} cannot align html_node_id with mapping_values"
                )
            code_selectors = [code for code, _ in mapping_pairs]
            mapping_values = [value for _, value in mapping_pairs]
            normalized.append(
                {
                    "standard_node_id": standard_id,
                    # `selectors` also contains binding/type/site metadata used
                    # for matching. DMP html_node_id must contain only the
                    # canonical HTML code identifier, e.g. code:S001_V003_L0017.
                    "html_node_id": ";".join(code_selectors),
                    "html_node_code": ";".join(code_selectors),
                    "mapping_values": ";".join(mapping_values),
                }
            )
            seen_standard.add(standard_id)
            seen_html.update(html_keys)
            seen_html.update(
                str(node.get("_source_node_key") or "")
                for node in selected_html
                if node.get("_source_node_key")
            )
        return normalized

    @classmethod
    def _section_scope_mapping(
        cls,
        standard_node: dict[str, Any],
        selected_nodes: list[dict[str, Any]],
        html_nodes: list[dict[str, Any]],
    ) -> dict[str, list[str]] | None:
        """Use a full HTML anchor range for a section-level standard text node."""
        if not cls._is_section_text_node(standard_node):
            return None

        selectors: list[str] = []
        scope_values: list[str] = []
        section_name = cls._section_text_name(standard_node)
        for node in selected_nodes:
            # A section-level standard node may span several HTML nodes, but
            # those nodes must belong to the same clinical section.  Without
            # this guard a broad summary anchor can absorb a later section
            # merely because its scope text contains that section's label.
            if not cls._section_name_matches(section_name, node):
                continue
            node_selectors = cls._canonical_code_selectors(
                [str(value) for value in (node.get("scope_selectors") or [])]
            )
            node_values = [
                value.strip()
                for value in str(node.get("scope_mapping_value") or "").split(";")
                if value.strip()
            ]
            if not node_selectors or not node_values:
                continue
            selectors.extend(node_selectors)
            scope_values.extend(node_values)

        selectors = list(dict.fromkeys(selectors))
        if not selectors or not scope_values:
            return None
        return {
            "selectors": selectors,
            "mapping_values": scope_values,
        }

    @staticmethod
    def _section_name_matches(section_name: str, node: dict[str, Any]) -> bool:
        """Match section labels without treating a broad parent as its child."""
        def compact(value: object) -> str:
            return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(value or "").casefold())

        expected = compact(section_name)
        if not expected:
            return True
        candidates = [node.get("section"), *(node.get("group_labels") or [])]
        actual = {compact(value) for value in candidates if compact(value)}
        return not actual or expected in actual

    @staticmethod
    def _is_section_text_node(node: dict[str, Any]) -> bool:
        path = [
            part.strip()
            for part in re.split(r"[/\\>|]+", str(node.get("path_text") or ""))
            if part.strip()
        ]
        if len(path) < 2:
            return False
        leaf = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", path[-1].casefold())
        return leaf in {"text", "content", "value", "文本", "内容", "值"}

    @staticmethod
    def _section_text_name(node: dict[str, Any]) -> str:
        path = [
            part.strip()
            for part in re.split(r"[/\\>|]+", str(node.get("path_text") or ""))
            if part.strip()
        ]
        return path[-2] if len(path) >= 2 else ""

    @classmethod
    def _expand_mapping_sources(
        cls,
        selected_nodes: list[dict[str, Any]],
        html_nodes: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Expand merged values into one canonical code/value pair per source node."""
        expanded: list[dict[str, Any]] = []
        seen_pairs: set[tuple[str, str]] = set()
        for selected in selected_nodes:
            for source in cls._mapping_source_rows(selected, html_nodes):
                pair = (source["selectors"][0], source["mapping_value"])
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                expanded.append(source)
        return expanded or selected_nodes

    @classmethod
    def _mapping_source_rows(
        cls,
        selected: dict[str, Any],
        html_nodes: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        selected_tokens = cls._mapping_value_tokens(selected)
        selected_codes = cls._node_code_selectors(selected)
        candidates = cls._mapping_source_candidates(selected, html_nodes, selected_codes)
        rows: list[dict[str, Any]] = []
        for token in selected_tokens:
            source = cls._best_mapping_source(token, candidates)
            if source:
                code = source["codes"][0]
                source_key = str(source["node"].get("node_key") or "")
            elif selected_codes:
                code = selected_codes[0]
                source_key = str(selected.get("node_key") or "")
            else:
                continue
            rows.append(
                {
                    "node_key": source_key,
                    "_source_node_key": source_key,
                    "selectors": [code],
                    "mapping_value": token,
                }
            )
        return rows

    @classmethod
    def _mapping_source_candidates(
        cls,
        selected: dict[str, Any],
        html_nodes: list[dict[str, Any]],
        selected_codes: list[str],
    ) -> list[dict[str, Any]]:
        selected_key = str(selected.get("node_key") or "")
        selected_names = {code.partition(":")[2].casefold() for code in selected_codes}
        result: list[dict[str, Any]] = []
        for candidate in html_nodes:
            if candidate.get("structural"):
                continue
            candidate_key = str(candidate.get("node_key") or "")
            anchor_names = {
                str(value).strip().casefold()
                for value in (candidate.get("anchor_path") or [])
                if str(value).strip()
            }
            group_codes = cls._canonical_code_selectors(
                [str(value) for value in (candidate.get("group_selectors") or [])]
            )
            group_names = {code.partition(":")[2].casefold() for code in group_codes}
            if (
                candidate_key != selected_key
                and not (selected_names & anchor_names)
                and not (selected_names & group_names)
            ):
                continue
            codes = cls._node_code_selectors(candidate)
            tokens = cls._mapping_value_tokens(candidate)
            if codes and tokens:
                result.append(
                    {
                        "node": candidate,
                        "codes": codes,
                        "tokens": tokens,
                        "selected": candidate_key == selected_key,
                        "depth": len(candidate.get("anchor_path") or []),
                    }
                )
        return result

    @staticmethod
    def _best_mapping_source(
        token: str, candidates: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        matching = [item for item in candidates if token in item["tokens"]]
        if not matching:
            return None
        return max(
            matching,
            key=lambda item: (
                str(item["node"].get("placeholder") or "").strip() == token,
                len(item["tokens"]) == 1,
                item["depth"],
                item["selected"],
                -int(item["node"].get("order") or 0),
            ),
        )

    @classmethod
    def _node_code_selectors(cls, node: dict[str, Any]) -> list[str]:
        selectors = [
            str(value).strip()
            for value in (node.get("selectors") or [])
            if str(value).strip()
        ]
        canonical = cls._canonical_code_selectors(selectors)
        if canonical:
            return canonical

        # A value node can inherit its HTML code from the surrounding anchor
        # range.  The parser exposes those inherited selectors separately so
        # that matching can use the whole group, but DMP needs one concrete
        # node code.  Prefer the most specific (deepest) inherited anchor.
        anchor_name = str(node.get("anchor_name") or "").strip()
        if anchor_name:
            canonical = cls._canonical_code_selectors([anchor_name])
            if canonical:
                return canonical

        group_selectors = [
            str(value).strip()
            for value in (node.get("group_selectors") or [])
            if str(value).strip()
        ]
        canonical = cls._canonical_code_selectors(group_selectors)
        return canonical[-1:] if canonical else []

    @staticmethod
    def _mapping_value_tokens(node: dict[str, Any]) -> list[str]:
        raw_value = str(node.get("mapping_value") or "").strip()
        tokens = [item.strip() for item in raw_value.split(";") if item.strip()]
        if tokens and not all(TemplateBindingCommitService._is_code_identifier(item) for item in tokens):
            return [item for item in tokens if not TemplateBindingCommitService._is_code_identifier(item)]

        placeholder = str(node.get("placeholder") or "").strip()
        if placeholder:
            return [placeholder]

        context_parts = [
            item.strip()
            for item in str(node.get("context_text") or "").split("；")
            if item.strip()
        ]
        for item in reversed(context_parts):
            if not TemplateBindingCommitService._is_code_identifier(item):
                return [item]

        for key in ("display_text", "local_label"):
            value = str(node.get(key) or "").strip()
            if value and not TemplateBindingCommitService._is_code_identifier(value):
                return [value]
        return []

    @staticmethod
    def _is_code_identifier(value: str) -> bool:
        return bool(_CODE_SELECTOR_RE.match(value) or _SCODE_SELECTOR_RE.match(value))

    @staticmethod
    def _canonical_code_selectors(selectors: list[str]) -> list[str]:
        """Return DMP-safe code selectors and discard binding metadata."""
        canonical: list[str] = []
        for selector in selectors:
            value = str(selector or "").strip()
            if not value or value.casefold() == "null":
                continue
            match = _CODE_SELECTOR_RE.match(value) or _SCODE_SELECTOR_RE.match(value)
            if match:
                code = match.group("code").strip()
            elif re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", value):
                # Older records and some HTML anchors contain the canonical
                # identifier without the `code:` prefix.
                code = value
            else:
                continue
            if not code:
                continue
            normalized = f"code:{code}"
            if normalized not in canonical:
                canonical.append(normalized)
        return canonical
