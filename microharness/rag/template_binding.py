"""
Two-Stage HTML-XML Template Binder
================================
Two-stage binding:
- Stage 1: HTML -> field list (LLM extraction)
- Stage 2: Fields -> XML node mapping (LLM matching)

XML templates are dynamically parsed, not hardcoded.
"""

import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Dict

from microharness.ollama import OllamaClient
from microharness.observability.logger import rag_logger


# ──────────────────────── Data Models ────────────────────────

@dataclass
class FieldBinding:
    html_field: str
    html_value: str
    xml_path: str
    confidence: float = 0.5


@dataclass
class DocumentBinding:
    html_file: str
    html_path: str
    xml_template: str
    xml_filepath: str
    stage1_model: str
    stage2_model: str
    stage3_model: str
    match_confidence: float
    field_bindings: List[FieldBinding] = field(default_factory=list)
    stage1_output: str = ""
    raw_response: str = ""


@dataclass
class BindingResult:
    created_at: str
    stage1_model: str
    stage2_model: str
    statistics: Dict
    bindings: List[DocumentBinding]


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


# ──────────────────────── Stage 1: HTML -> Field List ────────────────────────
# ✔ 改造重点：不写死字段，自适应提取 HTML 中所有字段
STAGE1_SYSTEM_PROMPT = """你是专业的电子病历解析工具。
从HTML病历中提取【所有存在的字段名】和【字段值】。

规则：
1. 不遗漏任何字段 - HTML中有什么就提取什么
2. 不编造字段 - 只提取真实存在的
3. 忽略样式、脚本、注释、隐藏内容
4. 空值填 ""，保持原文
5. 直接输出JSON数组，不要用markdown代码块包裹

输出格式：
[{"field":"字段名","value":"字段值"},...]
"""

STAGE1_USER_PROMPT = """请从下面病历中提取所有字段：

{cleaned_text}

直接输出JSON数组，不要其他内容：
"""


def stage1_extract(html_content: str, model: str, client: OllamaClient) -> str:
    """Stage 1: HTML -> field list (JSON format)"""
    cleaned = clean_html(html_content)
    rag_logger.debug(f"[Stage1] Cleaned length: {len(cleaned)}")

    user = STAGE1_USER_PROMPT.format(cleaned_text=cleaned[:6000])

    try:
        response = client.chat(
            messages=[
                {"role": "system", "content": STAGE1_SYSTEM_PROMPT},
                {"role": "user", "content": user}
            ],
            temperature=0.0
        )
        rag_logger.info(f"[Stage1] Output length: {len(response)}")
        return response.strip()
    except Exception as e:
        rag_logger.error(f"[Stage1] LLM call failed: {e}")
        return ""


def parse_stage1_output(output: str) -> Dict[str, str]:
    """Parse Stage1 JSON output."""
    fields = {}

    json_str = output.strip()
    if "```json" in json_str:
        parts = json_str.split("```json")
        if len(parts) >= 2:
            json_str = parts[1].split("```")[0].strip()
    elif "```" in json_str:
        parts = json_str.split("```")
        if len(parts) >= 2:
            json_str = parts[1].strip()

    try:
        data = json.loads(json_str)
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and "field" in item:
                    val = item.get("value")
                    fields[item["field"]] = val if val is not None else ""
        elif isinstance(data, dict):
            for k, v in data.items():
                if k not in ("template", "confidence"):
                    fields[k] = v if v is not None else ""
    except json.JSONDecodeError as e:
        rag_logger.warning(f"[Stage1] JSON parse failed: {e}")

    return fields


# ──────────────────────── XML Template Dynamic Parsing ────────────────────────

def parse_xml_template(xml_path: str) -> dict:
    """
    Dynamically parse XML template, extract all node paths and semantics.
    """
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

    def traverse(elem, path_prefix=""):
        tag = elem.tag.lower()
        current_path = f"{path_prefix}/{tag}" if path_prefix else tag

        if elem.text and elem.text.strip():
            sample = elem.text.strip()
            if len(sample) < 100:
                nodes[current_path] = {"sample": sample}

        for child in elem:
            traverse(child, current_path)

    traverse(root)
    rag_logger.info(f"Parsed XML: {filename}, nodes: {len(nodes)}")

    return {
        "filename": filename,
        "doc_type": doc_type,
        "title": title,
        "nodes": nodes
    }


def load_xml_templates(xml_dir: str = "data/临床文档模板") -> list:
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


# ──────────────────────── Stage 2: Fields -> XML Mapping ────────────────────────

def _parse_json_response(response: str) -> Optional[dict]:
    try:
        json_str = response.strip()
        # Extract JSON from markdown code block
        if "```json" in json_str:
            json_str = json_str.split("```json")[1].split("```")[0].strip()
        elif "```" in json_str:
            json_str = json_str.split("```")[1].strip()
        # Remove trailing commas
        json_str = re.sub(r',(\s*[}\]])', r'\1', json_str)
        # Handle null values - JSON null becomes Python None is fine
        return json.loads(json_str)
    except json.JSONDecodeError as e:
        rag_logger.warning(f"[Stage2] JSON parse failed: {e}, trying fallback...")
        # Fallback: try converting single quotes to double quotes
        try:
            # Replace single quotes with double quotes for JSON keys/values
            json_str_fallback = re.sub(r"'([^']*?)'\s*:", r'"\1":', json_str)  # key: value
            json_str_fallback = re.sub(r":\s*'([^']*?)'(?s*[,\]}])", r':"\1"\2', json_str_fallback)  # value
            json_str_fallback = re.sub(r':\s*null', ': null', json_str_fallback)  # null stays null
            return json.loads(json_str_fallback)
        except:
            return None
    except Exception as e:
        rag_logger.warning(f"[Stage2] Parse error: {e}")
        return None


# ──────────────────────── Stage 2: Template Matching ────────────────────────

def stage2_template_match(
    fields: Dict[str, str],
    client: OllamaClient,
    xml_templates: list,
    doc_type_hint: str = ""
) -> dict:
    """Stage 2: LLM determines which XML template matches the document."""
    if not xml_templates:
        return {"template": "", "confidence": 0.0, "bindings": []}

    # Build templates info for matching
    templates_info = ""
    for t in xml_templates:
        templates_info += f"\n【{t['filename']}】- 文档类型: {t.get('doc_type', '未知')} - 标题: {t.get('title', t['filename'])}\n"

    fields_text = "\n".join([f"- {k}: {v}" for k, v in fields.items()])

    system_template_match = """你是一个医疗文档分类器。
根据文档内容，从给定的模板列表中选择最匹配的模板文件名。

规则：
1. 只输出选中的模板文件名，不要其他内容
2. 精确匹配模板文件名
"""

    user_template_match = f"""【可用模板列表】
{templates_info}

【文档类型提示】
{doc_type_hint}

【病历字段示例】（前15个）
{fields_text[:800]}

请从上面的模板列表中选择一个最匹配的模板文件名。只输出文件名：
"""

    try:
        response = client.chat(
            messages=[
                {"role": "system", "content": system_template_match},
                {"role": "user", "content": user_template_match}
            ],
            temperature=0.0
        )
        raw_response = response.strip()
        rag_logger.info(f"[Stage2] Template match raw response: {raw_response}")

        # Extract filename from LLM response - find exact match in template list
        matched_template_name = ""
        for t in xml_templates:
            template_name_clean = re.sub(r'^[\d.]+\s*', '', t["filename"])
            if template_name_clean in raw_response or t["filename"] in raw_response:
                matched_template_name = t["filename"]
                break

        # Fallback: try to extract any .xml filename from response
        if not matched_template_name:
            xml_match = re.search(r'([^\s]+\.xml)', raw_response)
            if xml_match:
                candidate = xml_match.group(1)
                for t in xml_templates:
                    if candidate in t["filename"] or t["filename"] in candidate:
                        matched_template_name = t["filename"]
                        break

        rag_logger.info(f"[Stage2] Template matched: {matched_template_name}")
    except Exception as e:
        rag_logger.error(f"[Stage2] Template matching failed: {e}")
        matched_template_name = ""

    # Find the matched template object
    matched_template = None
    if matched_template_name:
        for t in xml_templates:
            if matched_template_name in t["filename"] or t["filename"] in matched_template_name:
                matched_template = t
                break

    if not matched_template:
        matched_template = xml_templates[0]
        rag_logger.warning(f"[Stage2] Template not found, using fallback: {matched_template['filename']}")

    return {"template": matched_template["filename"], "matched_template": matched_template}


# ──────────────────────── Stage 3: Field Binding ────────────────────────

def stage3_bind(
    fields: Dict[str, str],
    client: OllamaClient,
    xml_template: dict,
    batch_size: int = 10
) -> dict:
    """Stage 3: LLM binds structured fields to XML nodes."""
    if not xml_template or not fields:
        return {"template": "", "confidence": 0.0, "bindings": []}

    # Build valid paths for THIS TEMPLATE ONLY (with and without /text suffix)
    valid_paths = set()
    valid_paths_by_base = {}  # base_path -> full_path
    for path in xml_template["nodes"].keys():
        valid_paths.add(path.lower())
        # Extract base path (without /text suffix if present)
        base = path.lower().rstrip('/text')
        if base not in valid_paths_by_base:
            valid_paths_by_base[base] = path.lower()

    # Also add base paths themselves
    for base in list(valid_paths_by_base.keys()):
        valid_paths.add(base)

    templates_text = f"\n【{xml_template['filename']}】\n"
    for path, info in xml_template["nodes"].items():
        templates_text += f"  {path}: {info.get('sample', '')}\n"

    system = """你是医疗数据映射专家。
将提取的病历字段映射到 XML 模板节点。

重要规则：
1. 必须从节点列表中选择匹配的路径，尽量不要返回空字符串
2. 路径格式：带 /text 后缀的优先，如 clinicaldocument/docbody/patient/name/text
3. 如果找不到确切匹配，选择语义最接近的路径
"""

    all_bindings = []
    field_items = list(fields.items())

    # Process in batches
    for batch_start in range(0, len(field_items), batch_size):
        batch_end = min(batch_start + batch_size, len(field_items))
        batch_fields = dict(field_items[batch_start:batch_end])
        fields_text = "\n".join([f"- {k}: {v}" for k, v in batch_fields.items()])

        user = f"""【XML 模板节点列表】（只能使用这些路径，不要加前导斜杠）
{templates_text}

【待匹配字段】
{fields_text}

输出 JSON 数组格式（路径不能有前导斜杠，直接输出，不要其他内容）：
[{{"field":"字段名","xml_path":"clinicaldocument/docbody/..."}}]
"""

        try:
            response = client.chat(
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user}
                ],
                temperature=0.0
            )
            result = _parse_json_response(response)

            if isinstance(result, list):
                for b in result:
                    if isinstance(b, dict) and "xml_path" in b:
                        # Clean path - remove any ": value" suffix
                        raw_path = b["xml_path"].lower().strip()
                        path = re.sub(r':.*$', '', raw_path).strip()

                        # Accept empty string (field cannot be mapped)
                        if not path:
                            all_bindings.append(b)
                            continue

                        # Normalize path - remove leading/trailing slashes
                        path = path.strip('/')

                        # Check exact match
                        if path in valid_paths:
                            b["xml_path"] = path
                            all_bindings.append(b)
                            continue

                        # Try adding /text suffix
                        path_with_text = path if path.endswith('/text') else path + '/text'
                        if path_with_text in valid_paths:
                            b["xml_path"] = path_with_text
                            all_bindings.append(b)
                            continue

                        # Try removing /text suffix
                        path_without_text = path.rstrip('/text')
                        if path_without_text in valid_paths:
                            b["xml_path"] = path_without_text
                            all_bindings.append(b)
                            continue

                        rag_logger.warning(f"[Stage3] Invalid path ignored: {b['xml_path']}")

        except Exception as e:
            rag_logger.error(f"[Stage3] Batch {batch_start//batch_size +1} failed: {e}")

    confidence = len(all_bindings) / max(len(field_items), 1) if all_bindings else 0.0

    return {
        "template": xml_template["filename"],
        "confidence": confidence,
        "bindings": all_bindings
    }


# ──────────────────────── Combined Stage 2+3 ────────────────────────

def stage2_map(
    fields: Dict[str, str],
    model: str,
    client: OllamaClient,
    xml_templates: list,
    batch_size: int = 10,
    doc_type_hint: str = ""
) -> dict:
    """Combined Stage 2 (template match) + Stage 3 (field binding)."""
    if not xml_templates:
        return {"template": "", "confidence": 0.0, "bindings": []}

    if not fields:
        return {"template": "", "confidence": 0.0, "bindings": []}

    # Stage 2: Template matching
    stage2_result = stage2_template_match(fields, client, xml_templates, doc_type_hint)
    matched_template = stage2_result.get("matched_template")
    if not matched_template:
        return {"template": "", "confidence": 0.0, "bindings": []}

    # Stage 3: Field binding
    stage3_result = stage3_bind(fields, client, matched_template, batch_size)
    return stage3_result


# ──────────────────────── Two-Stage Binder ────────────────────────

class TwoStageBinder:
    def __init__(
        self,
        stage1_model: str = "qwen2.5:4b-instruct",
        stage2_model: str = "qwen2.5:7b",
        stage3_model: str = "qwen2.5:7b",
        xml_dir: str = "data/临床文档模板",
        stage1_timeout: int = 120,
        stage2_timeout: int = 120,
        stage3_timeout: int = 300
    ):
        self.stage1_model = stage1_model
        self.stage2_model = stage2_model
        self.stage3_model = stage3_model
        self.stage1_client = OllamaClient(model=stage1_model, timeout=stage1_timeout)
        self.stage2_client = OllamaClient(model=stage2_model, timeout=stage2_timeout)
        self.stage3_client = OllamaClient(model=stage3_model, timeout=stage3_timeout)
        self.xml_templates = load_xml_templates(xml_dir)
        rag_logger.info(f"TwoStageBinder init: S1={stage1_model}, S2={stage2_model}, S3={stage3_model}, XML templates={len(self.xml_templates)}")

    def bind_file(self, html_path: str) -> Optional[DocumentBinding]:
        html_path = Path(html_path)
        if not html_path.exists():
            rag_logger.error(f"HTML not found: {html_path}")
            return None

        try:
            html_content = html_path.read_text(encoding='utf-8', errors='replace')
        except Exception as e:
            rag_logger.error(f"Failed to read HTML: {e}")
            return None

        rag_logger.info(f"[TwoStage] Binding {html_path.name} (S1={self.stage1_model}, S2={self.stage2_model})")

        # Extract doc type hint from HTML filename/title
        import re
        doc_type_hint = html_path.stem # filename without extension
        # Try to extract from title tag if present
        title_match = re.search(r'<title>(.*?)</title>', html_content, re.IGNORECASE)
        if title_match:
            doc_type_hint = title_match.group(1)
        rag_logger.info(f"[TwoStage] Doc type hint: {doc_type_hint}")

        # Stage1
        stage1_output = stage1_extract(html_content, self.stage1_model, self.stage1_client)
        if not stage1_output:
            rag_logger.warning(f"[Stage1] No output: {html_path.name}")
            return DocumentBinding(
                html_file=html_path.name,
                html_path=str(html_path),
                xml_template="",
                xml_filepath="",
                stage1_model=self.stage1_model,
                stage2_model=self.stage2_model,
                match_confidence=0.0,
                stage1_output=""
            )

        fields = parse_stage1_output(stage1_output)
        rag_logger.info(f"[Stage1] Fields extracted: {len(fields)}")

        # Stage2: Template matching
        stage2_result = stage2_template_match(fields, self.stage2_client, self.xml_templates, doc_type_hint)
        matched_template_obj = stage2_result.get("matched_template")
        matched_template = stage2_result.get("template", "")

        # Stage3: Field binding
        stage3_result = stage3_bind(fields, self.stage3_client, matched_template_obj) if matched_template_obj else {"bindings": [], "confidence": 0.0}

        bindings = []
        for b in stage3_result.get("bindings", []):
            if isinstance(b, dict):
                fname = b.get("field", "")
                xpath = b.get("xml_path", "")
                val = fields.get(fname, "")
                bindings.append(FieldBinding(
                    html_field=fname,
                    html_value=val,
                    xml_path=xpath,
                    confidence=0.95
                ))

        return DocumentBinding(
            html_file=html_path.name,
            html_path=str(html_path),
            xml_template=matched_template,
            xml_filepath=matched_template,
            stage1_model=self.stage1_model,
            stage2_model=self.stage2_model,
            stage3_model=self.stage3_model,
            match_confidence=stage3_result.get("confidence", 0.0),
            field_bindings=bindings,
            stage1_output=stage1_output,
            raw_response=str(stage3_result)
        )

    def bind_directory(
        self,
        html_dir: str,
        output_path: str = None
    ) -> BindingResult:
        html_dir = Path(html_dir)
        if not html_dir.exists():
            rag_logger.error(f"HTML dir not found: {html_dir}")
            return None

        html_files = list(html_dir.glob("*.html"))
        rag_logger.info(f"[TwoStage] Found {len(html_files)} HTML files")

        bindings = []
        for hf in html_files:
            res = self.bind_file(str(hf))
            if res:
                bindings.append(res)

        matched = sum(1 for b in bindings if b.xml_template)
        statistics = {
            "total": len(html_files),
            "matched": matched,
            "unmatched": len(html_files)-matched,
            "match_rate": f"{matched/max(len(html_files),1)*100:.1f}%"
        }

        result = BindingResult(
            created_at=datetime.now().isoformat(),
            stage1_model=self.stage1_model,
            stage2_model=self.stage2_model,
            statistics=statistics,
            bindings=bindings
        )

        if output_path:
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, 'w', encoding='utf-8') as f:
                json.dump({
                    "created_at": result.created_at,
                    "stage1_model": result.stage1_model,
                    "stage2_model": result.stage2_model,
                    "statistics": result.statistics,
                    "bindings": [_b2dict(b) for b in result.bindings]
                }, f, ensure_ascii=False, indent=2)

        return result


def _b2dict(b: DocumentBinding) -> dict:
    return {
        "html_file": b.html_file,
        "xml_template": b.xml_template,
        "stage1_output": b.stage1_output,
        "match_confidence": b.match_confidence,
        "bindings": [asdict(bi) for bi in b.field_bindings]
    }