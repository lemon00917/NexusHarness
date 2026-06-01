"""
Three-Stage HTML-XML Binder
===========================
New binding flow:
1. LLM matches HTML to XML template
2. Parse XML template to get required fields
3. Convert HTML to structured text AND bind to XML nodes in one step

This is a separate implementation to avoid affecting the old 3-stage flow.
"""

import json
import re
from pathlib import Path
from typing import List, Optional, Dict

from microharness.ollama import OllamaClient
from microharness.observability.logger import rag_logger


# ──────────────────────── HTML Cleaning ────────────────────────

def clean_html(html: str) -> str:
    """Clean HTML, remove noise and extract plain text."""
    cleaned = html

    # Remove base64 images
    cleaned = re.sub(r'data:image/[^;]+;base64,[^\s"\'<>]+', '[图片]', cleaned)
    # Remove MathML
    cleaned = re.sub(r'<math[^>]*>.*?</math>', '', cleaned, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r'<annotation[^>]*>.*?</annotation>', '', cleaned, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r'<semantics[^>]*>.*?</semantics>', '', cleaned, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r'\s*xmlns="[^"]*"', '', cleaned)
    cleaned = re.sub(r'<!--[^>]*-->', '', cleaned)
    cleaned = re.sub(r'<script[^>]*>.*?</script>', '', cleaned, flags=re.DOTALL | re.IGNORECASE)

    # Extract text using HTMLParser
    from html.parser import HTMLParser
    class TextExtractor(HTMLParser):
        def __init__(self):
            super().__init__()
            self.texts = []
            self.skip = False
        def handle_starttag(self, tag, attrs):
            if tag in ('style', 'script'):
                self.skip = True
        def handle_endtag(self, tag):
            if tag in ('style', 'script'):
                self.skip = False
        def handle_data(self, data):
            if not self.skip:
                self.texts.append(data)
        def get_text(self):
            return ' '.join(self.texts)

    parser = TextExtractor()
    try:
        parser.feed(cleaned)
        text = parser.get_text()
    except:
        text = cleaned

    text = re.sub(r'[A-Za-z0-9+/]{80,}={0,2}', '', text)
    text = re.sub(r'data:image[^\s]+', '', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


# ──────────────────────── XML Template Parsing ────────────────────────

def load_xml_templates(xml_dir: str) -> list:
    """Load all XML templates from directory."""
    xml_path = Path(xml_dir)
    if not xml_path.exists():
        rag_logger.error(f"XML template dir not found: {xml_dir}")
        return []

    templates = []
    for xml_file in sorted(xml_path.glob("*.xml")):
        template_info = parse_xml_template(str(xml_file))
        if template_info["nodes"]:
            templates.append(template_info)

    rag_logger.info(f"Loaded {len(templates)} XML templates")
    return templates


def parse_xml_template(xml_path: str) -> dict:
    """Parse XML template, extract all node paths and semantics (including comments)."""
    path = Path(xml_path)
    if not path.exists():
        rag_logger.error(f"XML template not found: {xml_path}")
        return {"filename": "", "doc_type": "", "title": "", "nodes": {}}

    try:
        tree = ET.parse(path)
        root = tree.getroot()
    except Exception as e:
        rag_logger.error(f"XML parse failed: {xml_path}, {e}")
        return {"filename": "", "doc_type": "", "title": "", "nodes": {}}

    filename = path.name
    doc_type_elem = root.find('.//docType')
    doc_type = doc_type_elem.text if doc_type_elem is not None else ""
    title_elem = root.find('.//title')
    title = title_elem.text if title_elem.text else filename

    nodes = {}

    xml_content = path.read_text(encoding='utf-8')

    # Extract comments with their positions
    comments = {}
    for match in re.finditer(r'<!--\s*([^>]+?)\s*-->', xml_content):
        comments[match.start()] = match.group(1).strip()

    # Build a position-to-element mapping using a simple approach
    # For each element in the tree, find its approximate position in XML
    element_pos_map = {}  # path -> position

    # Use a stack to track current path and find positions
    stack = [(root, "", 0)]  # (element, path_prefix, last_pos)

    def find_element_pos(xml_text, target_path):
        """Find position of element by matching path through the XML text."""
        parts = target_path.split('/')
        pos = 0
        current_tag = parts[0] if parts else None

        # Find root element
        m = re.search(rf'<{current_tag}(?:\s[^>]*)?>', xml_text[pos:])
        if not m:
            return -1
        pos = m.start()

        for tag in parts[1:]:
            m = re.search(rf'<{tag}(?:\s[^>]*)?>', xml_text[pos + 1:])
            if not m:
                return -1
            pos += 1 + m.start()
        return pos

    def traverse(elem, path_prefix=""):
        tag = elem.tag
        current_path = f"{path_prefix}/{tag}" if path_prefix else tag

        sample = elem.text.strip() if elem.text and elem.text.strip() else ""

        # Find position and comment for this element
        elem_pos = find_element_pos(xml_content, current_path)
        semantic = ""
        if elem_pos != -1:
            for cpos in sorted(comments.keys(), reverse=True):
                if cpos < elem_pos:
                    semantic = comments[cpos]
                    break

        nodes[current_path] = {
            "sample": sample,
            "semantic": semantic
        }

        for child in elem:
            traverse(child, current_path)

    traverse(root)

    # Filter to keep only leaf nodes (paths that are not prefixes of other paths)
    leaf_nodes = {}
    sorted_paths = sorted(nodes.keys(), key=len)
    for path in sorted_paths:
        is_leaf = not any(p.startswith(path + "/") for p in nodes.keys())
        if is_leaf:
            leaf_nodes[path] = nodes[path]

    rag_logger.info(f"[Template] {len(nodes)} total nodes, {len(leaf_nodes)} leaf nodes")

    return {
        "filename": filename,
        "doc_type": doc_type,
        "title": title,
        "nodes": leaf_nodes
    }


# ──────────────────────── Stage 1: Template Matching ────────────────────────

STAGE1_SYSTEM_PROMPT = """你是一个医疗文档分类器。
根据文档内容，从给定的模板列表中选择最匹配的模板文件名。

规则：
1. 只输出选中的模板文件名，不要其他内容
2. 精确匹配模板文件名
"""


def stage1_match_template(
    html_content: str,
    templates: list,
    doc_type_hint: str,
    client: OllamaClient
) -> dict:
    """Stage 1: Match HTML document to XML template using LLM."""
    templates_info = "\n".join([f"- {t['filename']}" for t in templates])
    html_summary = clean_html(html_content)[:800]

    rag_logger.info(f"[Stage1] Template options: {[t['filename'] for t in templates]}")
    rag_logger.info(f"[Stage1] Doc type hint: {doc_type_hint}")

    user_prompt = f"""【可用模板列表】
{templates_info}

【文档类型提示】
{doc_type_hint}

【HTML内容摘要】
{html_summary}

请从上面的模板列表中选择一个最匹配的模板文件名。只输出文件名：
"""
    rag_logger.info(f"[Stage1] ====== SYSTEM PROMPT ======\n{STAGE1_SYSTEM_PROMPT}")
    rag_logger.info(f"[Stage1] ====== USER PROMPT ======\n{user_prompt}")

    try:
        response = client.chat([
            {"role": "system", "content": STAGE1_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt}
        ], temperature=0.0)
        matched_name = response.strip()
        rag_logger.info(f"[Stage1] LLM matched: {matched_name}")
        rag_logger.info(f"[Stage1] ====== LLM RESPONSE ======\n{response}")
    except Exception as e:
        rag_logger.error(f"[Stage1] LLM failed: {e}")
        matched_name = ""

    # Find matched template (ignore spaces in filename)
    matched_template = None
    matched_normalized = matched_name.replace(" ", "").replace("　", "")
    for t in templates:
        filename_normalized = t["filename"].replace(" ", "").replace("　", "")
        if matched_normalized in filename_normalized or filename_normalized in matched_normalized:
            matched_template = t
            break

    if not matched_template:
        matched_template = templates[0]
        rag_logger.warning(f"[Stage1] Using fallback: {matched_template['filename']}")

    return {"template": matched_template, "matched_name": matched_name}


# ──────────────────────── Stage 2: XML Field Parsing ────────────────────────

def stage2_parse_xml_fields(template: dict) -> list:
    """Stage 2: Get required fields from XML template."""
    return list(template["nodes"].keys())


# ──────────────────────── Stage 3: HTML to Structured + XML Binding ────────────────────────

STAGE3_SYSTEM_PROMPT = """你是一个医疗数据提取工具。
从HTML病历中提取字段值，绑定到XML节点。

重要规则：
1. 字段名必须是XML节点语义中的中文名称（只能使用下面列表中的名称）
2. 段落类内容（主诉、现病史、既往史，体格检查、专科检查等）必须提取完整原始文本，不拆分
3. xml_path必须完全匹配提供的节点路径，禁止编造路径
4. 只输出JSON数组，不要其他内容
5. 每个字段只输出一次
6. 禁止提取不在列表中的字段名
"""

STAGE3_RETRY_SYSTEM_PROMPT = """你是一个医疗数据提取工具。
根据提供的XML节点列表，从HTML病历中提取缺失的字段。

规则：
1. 字段名必须是XML节点语义中的中文名称
2. 段落类内容（主诉、现病史、既往史、体格检查、专科检查等）必须提取完整原始文本，不拆分
3. xml_path必须完全匹配提供的节点路径
4. 只输出JSON数组，不要其他内容
5. 每个字段只输出一次
6. 只提取提供的列表中的字段，不要额外字段
"""


def stage3_html_to_structured_with_binding(
    html_content: str,
    template: dict,
    client: OllamaClient
) -> list:
    """Stage 3: Extract Chinese field names and values from HTML, and bind to XML nodes in one step."""
    cleaned_html = clean_html(html_content)
    rag_logger.info(f"[Stage3] HTML cleaned length: {len(cleaned_html)}")

    # Build XML nodes text with semantic info for binding reference (no sample values to avoid hallucination)
    xml_fields_text = "\n".join([
        f"- {path} (语义: {info.get('semantic', '')})"
        for path, info in template["nodes"].items()
    ])

    rag_logger.info(f"[Stage3] XML nodes count: {len(template['nodes'])}")

    user_prompt = f"""【XML模板节点】（只允许使用这些路径，每个路径后的括号内是该节点的语义描述）
{xml_fields_text}

【HTML病历内容】：
{cleaned_html[:6000]}

任务：从HTML病历中提取字段值，绑定到对应的XML节点。每个字段只输出一次。

输出格式：
[{{"html_field":"字段名","value":"字段值","xml_path":"节点路径"}},...]"""
    rag_logger.info(f"[Stage3] ====== SYSTEM PROMPT ======\n{STAGE3_SYSTEM_PROMPT}")
    rag_logger.info(f"[Stage3] ====== USER PROMPT ======\n{user_prompt}")

    try:
        response = client.chat([
            {"role": "system", "content": STAGE3_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt}
        ], temperature=0.0)

        rag_logger.info(f"[Stage3] LLM response length: {len(response)}")
        rag_logger.info(f"[Stage3] ====== LLM RESPONSE ======\n{response}")

        # Clean response - remove markdown code blocks and model thinking artifacts
        json_str = response.strip()

        # Remove model thinking artifacts like <unused94>thought...
        json_str = re.sub(r'<unused\d+>thought[\s\S]*?```json\s*', '```json', json_str)
        json_str = re.sub(r'<unused\d+>```\s*', '', json_str)
        json_str = re.sub(r'<unused\d+>[^<]*', '', json_str)

        # Find JSON code block and extract it
        if "```json" in json_str:
            parts = json_str.split("```json")
            # Take the last part that looks like JSON
            for part in reversed(parts):
                part = part.strip()
                if part.startswith('[') or part.startswith('{'):
                    json_str = part.split("```")[0].strip()
                    break

        # Try to find JSON array in the response - use non-greedy matching
        json_match = re.search(r'\[\s*\{.*?\}\s*\]', json_str, re.DOTALL)
        if json_match:
            json_str = json_match.group(0)
        else:
            # Try alternative: find first [ and try to parse
            first_bracket = json_str.find('[')
            if first_bracket != -1:
                json_str = json_str[first_bracket:]
                # Try to find a complete closing bracket by locating matching ]
                # If JSON is incomplete/truncated, try to find last complete object
                last_complete = json_str.rfind('},')
                if last_complete != -1:
                    json_str = json_str[:last_complete + 1] + ']'
                    rag_logger.warning(f"[Stage3] JSON was truncated, attempting to parse: {json_str[:200]}...")

        # Fix common LLM output quirks: trailing colons in xml_path - handled in validation loop below

        try:
            bindings = json.loads(json_str)
        except json.JSONDecodeError as e:
            rag_logger.error(f"[Stage3] JSON parse failed: {e}")
            rag_logger.error(f"[Stage3] Raw response: {response[:500]}")
            return []

        rag_logger.info(f"[Stage3] Extracted and bound {len(bindings)} fields")

        # Validate and deduplicate bindings
        valid_paths = set(template["nodes"].keys())
        seen = set()
        validated = []
        for b in bindings:
            # Ensure all required keys exist
            if "html_field" not in b or "xml_path" not in b:
                continue
            if "value" not in b:
                b["value"] = ""

            xml_path = b.get("xml_path", "")
            html_field = b.get("html_field", "")
            key = (html_field, xml_path)

            # Skip if already seen
            if key in seen:
                continue
            seen.add(key)

            # Remove any trailing colons LLM might have added
            xml_path = xml_path.rstrip(":")
            if xml_path in valid_paths:
                b["xml_path"] = xml_path
                validated.append(b)
            else:
                # Try to find a matching valid path (case-insensitive)
                matched = False
                xml_path_lower = xml_path.lower()
                for valid_path in valid_paths:
                    if valid_path.lower() == xml_path_lower:
                        b["xml_path"] = valid_path
                        validated.append(b)
                        matched = True
                        break
                if not matched:
                    rag_logger.warning(f"[Stage3] Invalid xml_path: {xml_path}, skipping")

        rag_logger.info(f"[Stage3] Validated {len(validated)} bindings out of {len(bindings)} (deduped)")
        return validated
    except json.JSONDecodeError as e:
        rag_logger.error(f"[Stage3] JSON parse failed: {e}")
        rag_logger.error(f"[Stage3] Raw response: {response[:500]}")
        return []
    except Exception as e:
        rag_logger.error(f"[Stage3] LLM failed: {e}")
        return []


# ──────────────────────── Three-Stage Binder ────────────────────────

class ThreeStageBinder:
    """Three-stage HTML-XML binder (merged Stage3+Stage4)."""

    def __init__(
        self,
        stage1_model: str = "qwen2.5:7b",
        stage2_model: str = "qwen2.5:7b",
        stage3_model: str = "qwen2.5:7b",
        xml_dir: str = "data/临床文档模板",
        timeout: int = 120
    ):
        self.stage1_model = stage1_model
        self.stage2_model = stage2_model
        self.stage3_model = stage3_model
        self.timeout = timeout
        self.templates = load_xml_templates(xml_dir)

    def bind_file(self, html_path: str) -> Optional[dict]:
        """Bind HTML file using 3-stage flow."""
        html_path = Path(html_path)
        if not html_path.exists():
            rag_logger.error(f"HTML not found: {html_path}")
            return None

        try:
            html_content = html_path.read_text(encoding='utf-8', errors='replace')
        except Exception as e:
            rag_logger.error(f"Failed to read HTML: {e}")
            return None

        # Extract doc type hint from filename
        doc_type_hint = html_path.stem

        # Create clients
        stage1_client = OllamaClient(model=self.stage1_model, timeout=self.timeout)
        stage3_client = OllamaClient(model=self.stage3_model, timeout=self.timeout * 2)

        # Stage 1: Template matching
        rag_logger.info(f"[ThreeStage] Stage 1: Template matching")
        stage1_result = stage1_match_template(
            html_content, self.templates, doc_type_hint, stage1_client
        )
        matched_template = stage1_result["template"]

        # Stage 2: Parse XML fields with semantic info
        rag_logger.info(f"[ThreeStage] Stage 2: XML field parsing")
        xml_fields = {
            path: {"semantic": info.get("semantic", ""), "sample": info.get("sample", "")}
            for path, info in matched_template["nodes"].items()
        }

        # Stage 3: HTML to structured with XML binding
        rag_logger.info(f"[ThreeStage] Stage 3: HTML to structured + XML binding")
        bindings = stage3_html_to_structured_with_binding(
            html_content, matched_template, stage3_client
        )

        # Check for missing fields and retry if needed
        missing_fields = self._find_missing_fields(bindings, matched_template, html_content)
        filled_fields = []
        if missing_fields:
            rag_logger.info(f"[ThreeStage] Retrying with missing fields: {missing_fields}")
            retry_bindings = self._retry_extract_fields(
                missing_fields, matched_template, html_content, stage3_client
            )
            if retry_bindings:
                seen = {(b["html_field"], b["xml_path"]) for b in bindings}
                retry_count = 0
                for b in retry_bindings:
                    if (b["html_field"], b["xml_path"]) not in seen:
                        bindings.append(b)
                        seen.add((b["html_field"], b["xml_path"]))
                        retry_count += 1
                        filled_fields.append(b["html_field"])
                rag_logger.info(f"[ThreeStage] After retry: +{retry_count} bindings")

        # Mark which missing fields were filled
        for m in missing_fields:
            m["filled"] = m["semantic"] in filled_fields

        # Build html_fields from bindings for compatibility
        html_fields = [{"field": b["html_field"], "value": b["value"]} for b in bindings]

        return {
            "html_file": html_path.name,
            "template": matched_template["filename"],
            "xml_fields": xml_fields,
            "html_fields": html_fields,
            "bindings": bindings,
            "missing_fields": missing_fields
        }

    def _find_missing_fields(self, current_bindings, template, html_content):
        """Find fields from XML that are not in current bindings."""
        current_paths = {b["xml_path"] for b in current_bindings}
        current_html_fields = {b.get("html_field", "") for b in current_bindings}
        missing = []
        cleaned_html = clean_html(html_content).lower()

        for path, info in template["nodes"].items():
            if path in current_paths:
                continue
            semantic = info.get("semantic", "")
            if not semantic:
                continue
            if semantic.lower() in cleaned_html and semantic not in current_html_fields:
                missing.append({"path": path, "semantic": semantic})

        rag_logger.info(f"[ThreeStage] Found {len(missing)} missing fields")
        return missing

    def _retry_extract_fields(self, missing_fields, template, html_content, client):
        """Retry extraction for missing fields only."""
        cleaned_html = clean_html(html_content)

        fields_text = "\n".join([
            f"- {f['path']} (语义: {f['semantic']})"
            for f in missing_fields
        ])

        user_prompt = f"""【缺失的XML节点】（需要提取这些字段）
{fields_text}

【HTML病历内容】：
{cleaned_html[:6000]}

任务：从HTML中找出这些字段的完整原始值，绑定到对应节点。

输出格式：
[{{"html_field":"字段名","value":"字段值","xml_path":"节点路径"}},...]

只输出JSON，不要其他内容：
"""

        try:
            response = client.chat([
                {"role": "system", "content": STAGE3_RETRY_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt}
            ], temperature=0.0)

            json_str = response.strip()
            # Remove thinking artifacts
            json_str = re.sub(r'<unused\d+>thought[\s\S]*?```json\s*', '```json', json_str)
            json_str = re.sub(r'<unused\d+>```\s*', '', json_str)
            json_str = re.sub(r'<unused\d+>[^<]*', '', json_str)

            if "```json" in json_str:
                parts = json_str.split("```json")
                for part in reversed(parts):
                    part = part.strip()
                    if part.startswith('[') or part.startswith('{'):
                        json_str = part.split("```")[0].strip()
                        break
            elif "```" in json_str:
                json_str = json_str.split("```")[1].split("```")[0].strip()

            # Handle truncation
            json_match = re.search(r'\[\s*\{.*?\}\s*\]', json_str, re.DOTALL)
            if json_match:
                json_str = json_match.group(0)
            else:
                first_bracket = json_str.find('[')
                if first_bracket != -1:
                    json_str = json_str[first_bracket:]
                    last_complete = json_str.rfind('},')
                    if last_complete != -1:
                        json_str = json_str[:last_complete + 1] + ']'

            retry_bindings = json.loads(json_str)

            valid_paths = set(template["nodes"].keys())
            validated = []
            for b in retry_bindings:
                xml_path = b.get("xml_path", "").rstrip(":")
                if xml_path in valid_paths:
                    b["xml_path"] = xml_path
                    validated.append(b)

            return validated
        except Exception as e:
            rag_logger.error(f"[ThreeStage] Retry failed: {e}")
            return []


# ──────────────────────── Alias for backwards compatibility ────────────────────────
FourStageBinder = ThreeStageBinder


# ──────────────────────── XML Template Parsing (needs import) ────────────────────────
import xml.etree.ElementTree as ET