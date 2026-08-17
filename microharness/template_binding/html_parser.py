"""Decode and extract bindable nodes from database HTML templates."""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from dataclasses import asdict, dataclass, field
from html.parser import HTMLParser
from typing import Any


class HtmlTemplateDecodeError(ValueError):
    """Raised when html_info is not valid MIME Base64 UTF-8 HTML."""


_SQUARE_PLACEHOLDER_RE = re.compile(r"\[[^\[\]\r\n]{1,80}\]")
_BRACE_PLACEHOLDER_RE = re.compile(r"\{[^{}\r\n]{1,300}\}")
_CODE_RE = re.compile(r"(?<![\w-])code:[^\s;\"'<>]+")
_HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_SKIPPED_CONTENT_TAGS = {"script", "style"}
_VOID_ELEMENTS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
_VALUE_LEAF_NAMES = {"value", "text", "content", "值", "文本", "内容"}


def _placeholders(value: str) -> list[str]:
    square = [
        item
        for item in _SQUARE_PLACEHOLDER_RE.findall(value)
        if not item[1:-1].strip().isdigit()
    ]
    return square + _BRACE_PLACEHOLDER_RE.findall(value)


def _dynamic_field_placeholder(tag: str, attrs: dict[str, str]) -> str:
    if tag != "field" or attrs.get("type", "").strip().casefold() != "dynamic_field":
        return ""
    param = attrs.get("param", "").strip()
    if not param:
        return ""
    existing = _placeholders(param)
    return existing[0] if existing else f"[{param}]"


def _is_code_identifier(value: str) -> bool:
    return bool(re.fullmatch(r"(?:s?code):[^\s;]+", str(value or "").strip(), re.IGNORECASE))


def _mapping_text(value: str) -> str:
    return " ".join(str(value or "").replace("\xa0", " ").split()).strip()


def _parse_binding_tag(value: str) -> dict[str, Any]:
    """Parse generic ``$Kind#KEY:value`` metadata used by HTML templates."""
    parts = [part.strip() for part in str(value or "").split("#") if part.strip()]
    if not parts:
        return {}
    metadata: dict[str, Any] = {}
    first = parts[0]
    if first.startswith("$"):
        metadata["binding"] = first[1:].strip()
        parts = parts[1:]
    for part in parts:
        key, separator, item_value = part.partition(":")
        if separator and key.strip():
            metadata[key.strip().upper()] = item_value.strip()
    return metadata


def _uname_parts(uname: str) -> list[str]:
    return [part.strip().strip(":：") for part in str(uname or "").split(".") if part.strip().strip(":：")]


def _uname_display_and_section(uname: str) -> tuple[str, str]:
    parts = _uname_parts(uname)
    if not parts:
        return "", ""
    display_index = len(parts) - 1
    if parts[display_index].casefold() in _VALUE_LEAF_NAMES and display_index > 0:
        display_index -= 1
    display = parts[display_index]
    section = ".".join(parts[:display_index])
    return display, section


def _metadata_selectors(metadata: dict[str, Any]) -> list[str]:
    selectors: list[str] = []
    binding = str(metadata.get("binding") or "").strip()
    if binding:
        selectors.append(f"binding:{binding}")
    for key, value in metadata.items():
        if key == "binding" or not value or key == "UNAME":
            continue
        normalized_key = str(key).lower()
        selectors.append(f"{normalized_key}:{value}")
        if key == "SCODE":
            selectors.extend([str(value), f"code:{value}"])
    return selectors


@dataclass
class HtmlNode:
    node_key: str
    selectors: list[str]
    section: str
    placeholder: str
    display_text: str
    context_text: str
    mapping_value: str
    order: int
    tag: str = ""
    anchor_name: str = ""
    anchor_type: str = ""
    usage: str = ""
    pair_status: str = ""
    anchor_path: list[str] = field(default_factory=list)
    group_selectors: list[str] = field(default_factory=list)
    group_labels: list[str] = field(default_factory=list)
    local_label: str = ""
    structural: bool = False
    scope_selectors: list[str] = field(default_factory=list)
    scope_mapping_value: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def decode_html_info(value: str | bytes) -> str:
    """Decode Base64 while accepting MIME line breaks but rejecting bad bytes."""
    try:
        raw = value.encode("ascii", errors="strict") if isinstance(value, str) else value
    except UnicodeEncodeError as exc:
        raise HtmlTemplateDecodeError(f"html_info is not ASCII Base64: {exc}") from exc
    compact = re.sub(rb"\s+", b"", raw)
    try:
        decoded = base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HtmlTemplateDecodeError(f"invalid Base64 html_info: {exc}") from exc
    try:
        return decoded.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HtmlTemplateDecodeError(f"html_info is not UTF-8: {exc}") from exc


class _NodeCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.nodes: list[HtmlNode] = []
        self.warnings: list[str] = []
        self.section = ""
        self._order = 0
        self._tag_stack: list[str] = []
        self._element_placeholders: list[set[str]] = []
        self._element_node_indexes: list[list[int]] = []
        self._heading_tag = ""
        self._heading_text: list[str] = []
        self._open_anchors: dict[str, list[int]] = {}
        self._active_anchors: list[dict[str, Any]] = []
        self._finalized_anchors: list[dict[str, Any]] = []
        self._pending_label = ""
        self._pending_label_path: tuple[str, ...] = ()

    def handle_starttag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attrs = {key.lower(): value or "" for key, value in attrs_list}
        self._tag_stack.append(tag)
        self._element_placeholders.append(set())
        self._element_node_indexes.append([])
        if tag in _HEADINGS:
            self._heading_tag = tag
            self._heading_text = []

        if any(item in _SKIPPED_CONTENT_TAGS for item in self._tag_stack):
            return

        selectors = _CODE_RE.findall(attrs.get("style", ""))
        metadata = _parse_binding_tag(attrs.get("tag", ""))
        selectors.extend(_metadata_selectors(metadata))
        placeholders = _placeholders(attrs.get("value", ""))
        uname = str(metadata.get("UNAME") or "").strip()
        metadata_placeholder = f"{{{uname}}}" if uname else ""
        dynamic_placeholder = _dynamic_field_placeholder(tag, attrs)
        placeholder = metadata_placeholder or (placeholders[0] if placeholders else dynamic_placeholder)
        anchor_name = attrs.get("name", "") if tag == "a" else ""
        anchor_type = attrs.get("type", "").lower() if tag == "a" else ""
        usage = str(metadata.get("TYPE") or (attrs.get("usage", "") if tag == "a" else ""))
        is_anchor_start = bool(anchor_name and anchor_type == "start")
        is_anchor_end = bool(anchor_name and anchor_type == "end")
        if is_anchor_end:
            self._close_active_anchor(anchor_name)
        if anchor_name:
            selectors.append(anchor_name)
        if selectors or placeholder or anchor_type or usage:
            metadata_display, metadata_section = _uname_display_and_section(uname)
            if metadata_display:
                display = metadata_display
            elif placeholder:
                display = placeholder
            elif anchor_name:
                display = anchor_name
            elif selectors:
                display = selectors[0]
            else:
                display = usage
            node_index = self._emit(
                tag=tag,
                selectors=selectors,
                placeholder=placeholder,
                display=display,
                context="",
                anchor_name=anchor_name,
                anchor_type=anchor_type,
                usage=usage,
                section=metadata_section or None,
                structural=bool(anchor_name and anchor_type in {"start", "end"}),
            )
            self._element_node_indexes[-1].append(node_index)
            if placeholder:
                self._element_placeholders[-1].add(placeholder)
            if is_anchor_start:
                parent_names = self._active_anchor_names()
                self._open_anchors.setdefault(anchor_name, []).append(node_index)
                self._active_anchors.append(
                    {
                        "name": anchor_name,
                        "usage": usage,
                        "label": "",
                        "start_index": node_index,
                        "member_indexes": [],
                        "root_index": None,
                        "parent_names": parent_names,
                        "mapping_tokens": [],
                    }
                )
                if str(usage).strip() == "1":
                    self._pending_label = ""
                    self._pending_label_path = ()
            elif is_anchor_end:
                starts = self._open_anchors.get(anchor_name, [])
                if starts:
                    start_index = starts.pop()
                    self.nodes[start_index].pair_status = "paired"
                    self.nodes[node_index].pair_status = "paired"
                else:
                    self.nodes[node_index].pair_status = "missing_start"
                    self.warnings.append(f"anchor {anchor_name} has end without start")
        if dynamic_placeholder:
            self._record_active_anchor_text(dynamic_placeholder)
        if tag in _VOID_ELEMENTS:
            self._tag_stack.pop()
            self._element_placeholders.pop()
            self._element_node_indexes.pop()

    def handle_data(self, data: str) -> None:
        if any(item in _SKIPPED_CONTENT_TAGS for item in self._tag_stack):
            return
        self._record_active_anchor_text(data)
        text = " ".join(data.split())
        if self._heading_tag and text:
            self._heading_text.append(text)
        if not text:
            return
        placeholders = _placeholders(data)
        current_indexes = self._element_node_indexes[-1] if self._element_node_indexes else []
        if placeholders and current_indexes:
            target = self.nodes[current_indexes[-1]]
            for placeholder in placeholders:
                if any(placeholder in values for values in self._element_placeholders):
                    continue
                self._merge_placeholder(target, placeholder, text)
                if self._element_placeholders:
                    self._element_placeholders[-1].add(placeholder)
            return
        for placeholder in placeholders:
            if any(placeholder in values for values in self._element_placeholders):
                continue
            raw_value = placeholder[1:-1].strip()
            display, section = _uname_display_and_section(raw_value) if placeholder.startswith("{") else (placeholder, "")
            self._emit(
                tag=self._tag_stack[-1] if self._tag_stack else "",
                selectors=[],
                placeholder=placeholder,
                display=display or placeholder,
                context=text,
                section=section or None,
            )
            if self._element_placeholders:
                self._element_placeholders[-1].add(placeholder)
        if placeholders:
            return

        if current_indexes:
            for index in current_indexes:
                self._merge_static_text(index, text)
            return

        label = self._clean_label(text)
        if label and self._looks_like_field_label(text):
            self._pending_label = label
            self._pending_label_path = tuple(self._active_anchor_names())

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._heading_tag == tag:
            heading = " ".join(self._heading_text).strip()
            if heading:
                self.section = heading
            self._heading_tag = ""
            self._heading_text = []
        if self._tag_stack:
            if self._tag_stack[-1] == tag:
                self._tag_stack.pop()
                self._element_placeholders.pop()
                self._element_node_indexes.pop()
            elif tag in self._tag_stack:
                reverse_index = self._tag_stack[::-1].index(tag)
                index = len(self._tag_stack) - reverse_index - 1
                del self._tag_stack[index]
                del self._element_placeholders[index]
                del self._element_node_indexes[index]

    def close(self) -> None:
        super().close()
        for anchor_name, indexes in self._open_anchors.items():
            for index in indexes:
                self.nodes[index].pair_status = "missing_end"
            if indexes:
                self.warnings.append(f"anchor {anchor_name} has start without end")
        self._apply_anchor_family_scopes()

    def _emit(
        self,
        *,
        tag: str,
        selectors: list[str],
        placeholder: str,
        display: str,
        context: str,
        anchor_name: str = "",
        anchor_type: str = "",
        usage: str = "",
        section: str | None = None,
        structural: bool = False,
    ) -> int:
        self._order += 1
        unique_selectors = list(dict.fromkeys(item for item in selectors if item))
        active_names = self._active_anchor_names()
        active_labels = self._active_anchor_labels()
        active_selectors = [item for name in active_names for item in (name, f"code:{name}")]
        local_label = ""
        if not structural and self._pending_label_applies(active_names):
            local_label = self._pending_label
            self._pending_label = ""
            self._pending_label_path = ()
        resolved_section = self.section if section is None else section
        if not resolved_section and active_labels:
            resolved_section = " / ".join(active_labels)
        node = HtmlNode(
            node_key=f"html-node-{self._order}",
            selectors=unique_selectors,
            section=resolved_section,
            placeholder=placeholder,
            display_text=display,
            context_text=context,
            mapping_value=placeholder or ("" if _is_code_identifier(display) else display),
            order=self._order,
            tag=tag,
            anchor_name=anchor_name,
            anchor_type=anchor_type,
            usage=usage,
            anchor_path=active_names,
            group_selectors=list(dict.fromkeys(active_selectors)),
            group_labels=active_labels,
            local_label=local_label,
            structural=structural,
        )
        self.nodes.append(node)
        node_index = len(self.nodes) - 1
        if not structural:
            for anchor in self._active_anchors:
                anchor["member_indexes"].append(node_index)
                direct_selectors = set(unique_selectors)
                if f"code:{anchor['name']}" in direct_selectors or anchor["name"] in direct_selectors:
                    anchor["root_index"] = node_index
        return node_index

    def _active_anchor_names(self) -> list[str]:
        return [str(item["name"]) for item in self._active_anchors if item.get("name")]

    def _active_anchor_labels(self) -> list[str]:
        return [str(item["label"]) for item in self._active_anchors if item.get("label")]

    def _close_active_anchor(self, anchor_name: str) -> None:
        for index in range(len(self._active_anchors) - 1, -1, -1):
            if self._active_anchors[index].get("name") != anchor_name:
                continue
            anchor = self._active_anchors.pop(index)
            self._finalize_anchor(anchor)
            return

    def _finalize_anchor(self, anchor: dict[str, Any]) -> None:
        root_index = anchor.get("root_index")
        scope_target_index = root_index if root_index is not None else anchor.get("start_index")
        scope_tokens = [
            str(value).strip()
            for value in (anchor.get("mapping_tokens") or [])
            if str(value).strip()
        ]
        scope_selector = f"code:{anchor['name']}"
        if scope_target_index is not None:
            scope_target = self.nodes[int(scope_target_index)]
            scope_target.scope_selectors = [scope_selector]
            scope_target.scope_mapping_value = ";".join(scope_tokens)
        self._finalized_anchors.append(
            {
                "name": str(anchor.get("name") or ""),
                "usage": str(anchor.get("usage") or ""),
                "parent_names": list(anchor.get("parent_names") or []),
                "start_index": int(anchor.get("start_index") or 0),
                "root_index": root_index,
                "scope_target_index": scope_target_index,
                "mapping_tokens": scope_tokens,
            }
        )
        if root_index is None:
            return
        root = self.nodes[int(root_index)]
        all_mapping_values: list[str] = []
        placeholder_mapping_values: list[str] = []
        context_values: list[str] = []
        for member_index in anchor.get("member_indexes") or []:
            member = self.nodes[int(member_index)]
            values = [value.strip() for value in str(member.mapping_value or "").split(";") if value.strip()]
            all_mapping_values.extend(values)
            if member.placeholder:
                placeholder_mapping_values.extend(values)
            for value in (member.local_label, member.display_text, member.context_text):
                value = str(value or "").strip()
                if value and value not in context_values:
                    context_values.append(value)
        mapping_values = placeholder_mapping_values if root.placeholder and placeholder_mapping_values else all_mapping_values
        if mapping_values:
            root.mapping_value = ";".join(dict.fromkeys(mapping_values))
        if context_values:
            root.context_text = "；".join(context_values)[:1200]

    def _record_active_anchor_text(self, value: str) -> None:
        token = _mapping_text(value)
        if not token:
            return
        for anchor in self._active_anchors:
            anchor["mapping_tokens"].append(token)

    def _apply_anchor_family_scopes(self) -> None:
        """Combine sibling anchors that encode one major clinical section."""
        finalized = sorted(self._finalized_anchors, key=lambda item: item["start_index"])
        for anchor in finalized:
            root_index = anchor.get("root_index")
            name = str(anchor.get("name") or "")
            family_match = re.match(r"^(S\d{3})", name, re.IGNORECASE)
            if root_index is None or str(anchor.get("usage") or "") != "1" or not family_match:
                continue
            family = family_match.group(1).casefold()
            siblings = [
                item
                for item in finalized
                if item.get("parent_names") == anchor.get("parent_names")
                and (
                    item is anchor
                    or str(item.get("name") or "").casefold().startswith(f"{family}_")
                )
            ]
            if len(siblings) <= 1:
                continue
            selectors = [
                f"code:{item['name']}"
                for item in siblings
                if str(item.get("name") or "").strip()
            ]
            tokens = [
                token
                for item in siblings
                for token in (item.get("mapping_tokens") or [])
                if str(token).strip()
            ]
            root = self.nodes[int(root_index)]
            root.scope_selectors = list(dict.fromkeys(selectors))
            root.scope_mapping_value = ";".join(tokens)

    def _merge_placeholder(self, node: HtmlNode, placeholder: str, context: str) -> None:
        raw_value = placeholder[1:-1].strip()
        display, explicit_section = (
            _uname_display_and_section(raw_value) if placeholder.startswith("{") else (placeholder, "")
        )
        node.placeholder = placeholder
        node.display_text = display or placeholder
        node.mapping_value = placeholder
        node.context_text = context
        if explicit_section:
            node.section = explicit_section
        elif not node.section:
            labels = self._active_anchor_labels()
            if labels:
                node.section = " / ".join(labels)
        if not node.local_label and self._pending_label_applies(node.anchor_path):
            node.local_label = self._pending_label
            self._pending_label = ""
            self._pending_label_path = ()

    def _merge_static_text(self, node_index: int, text: str) -> None:
        node = self.nodes[node_index]
        label = self._clean_label(text)
        if not label:
            return
        node.context_text = text
        direct_selectors = set(node.selectors)
        matching_anchor = next(
            (
                anchor
                for anchor in reversed(self._active_anchors)
                if f"code:{anchor['name']}" in direct_selectors or anchor["name"] in direct_selectors
            ),
            None,
        )
        if matching_anchor is not None:
            node.display_text = label
            node.local_label = label
            if not node.placeholder:
                node.mapping_value = _mapping_text(text)
            self._set_anchor_label(matching_anchor, label)
            return
        if self._looks_like_field_label(text):
            node.local_label = label
            if not node.placeholder:
                node.display_text = label
                node.mapping_value = _mapping_text(text)

    def _set_anchor_label(self, anchor: dict[str, Any], label: str) -> None:
        if not label:
            return
        anchor["label"] = label
        labels = self._active_anchor_labels()
        section = " / ".join(labels)
        for member_index in anchor.get("member_indexes") or []:
            member = self.nodes[int(member_index)]
            member.group_labels = labels.copy()
            if not member.section or member.section == self.section:
                member.section = section

    @staticmethod
    def _clean_label(text: str) -> str:
        compact = " ".join(str(text or "").replace("\xa0", " ").split()).strip()
        compact = compact.strip(" :：;；,，。()（）[]【】")
        return compact[:120]

    @staticmethod
    def _looks_like_field_label(text: str) -> bool:
        compact = " ".join(str(text or "").replace("\xa0", " ").split()).strip()
        if not compact or len(compact) > 120:
            return False
        return compact.endswith((":", "："))

    def _pending_label_applies(self, active_names: list[str]) -> bool:
        if not self._pending_label:
            return False
        pending = self._pending_label_path
        current = tuple(active_names)
        return len(pending) <= len(current) and current[: len(pending)] == pending


def extract_html_nodes(html: str) -> dict[str, Any]:
    parser = _NodeCollector()
    parser.feed(html)
    parser.close()
    return {
        "nodes": [node.to_dict() for node in parser.nodes],
        "warnings": parser.warnings,
    }


def parse_html_info(value: str | bytes, include_html: bool = False) -> dict[str, Any]:
    html = decode_html_info(value)
    extracted = extract_html_nodes(html)
    result = {
        "html_sha256": hashlib.sha256(html.encode("utf-8")).hexdigest(),
        "html_length": len(html),
        "node_count": len(extracted["nodes"]),
        **extracted,
    }
    if include_html:
        result["html"] = html
    return result
