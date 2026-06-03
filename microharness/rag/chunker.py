"""
Document Chunker for NexusHarness RAG
=====================================
Splits documents into chunks for better retrieval.
"""

import re
from typing import List, Dict, Optional


# ──────────────────────── Configuration ────────────────────────

DEFAULT_CHUNK_SIZE = 1500
DEFAULT_OVERLAP = 200
PREVIEW_LENGTH = 5000

# Sentence-ending patterns for intelligent break points
SENTENCE_BOUNDARIES = r'[.。!?！？]+'


# ──────────────────────── Public API ────────────────────────

def chunk_text(
    text: str,
    mode: str = "length",
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_OVERLAP
) -> List[str]:
    """
    Split text into chunks based on the specified mode.

    Args:
        text: Input text to chunk
        mode: Chunking strategy
            - "length": Split by character count with smart boundaries
            - "chapter": Split by markdown/HTML headings (h2, h3)
        chunk_size: Maximum characters per chunk (length mode only)
        overlap: Overlapping characters between chunks (length mode only)

    Returns:
        List of text chunks

    Raises:
        ValueError: If mode is not supported
    """
    if mode == "chapter":
        return _chunk_by_chapter(text)
    elif mode == "length":
        return _chunk_by_length(text, chunk_size, overlap)
    elif mode == "llm":
        raise ValueError("LLM chunking requires async rag.chunk_with_llm() - cannot use chunk_text() directly")
    else:
        raise ValueError(f"Unknown chunking mode: '{mode}'. Use 'length', 'chapter', or 'llm'.")


def preview_chunks(
    text: str,
    mode: str = "length",
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_OVERLAP,
    preview_length: int = PREVIEW_LENGTH
) -> List[Dict]:
    """
    Preview how text would be chunked without full processing.

    Args:
        text: Input text to preview
        mode: Chunking strategy ("length" or "chapter")
        chunk_size: Maximum characters per chunk
        overlap: Overlap between chunks
        preview_length: Characters to show in preview

    Returns:
        List of dictionaries with chunk metadata:
            - chunk_index: Position in chunk list
            - char_count: Total characters in chunk
            - preview: First {preview_length} characters
    """
    chunks = chunk_text(text, mode, chunk_size=chunk_size, overlap=overlap)

    return [
        {
            "chunk_index": idx,
            "char_count": len(chunk),
            "preview": _truncate_text(chunk, preview_length)
        }
        for idx, chunk in enumerate(chunks)
    ]


# ──────────────────────── Length-based Chunking ────────────────────────

def _chunk_by_length(
    text: str,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_OVERLAP
) -> List[str]:
    """
    Split text into fixed-size chunks with intelligent boundary detection.

    Strategy:
    1. Divide text into chunk_size segments
    2. At each break point, look backward for a sentence boundary
    3. Include overlap between chunks for context preservation

    Args:
        text: Text to split
        chunk_size: Target characters per chunk
        overlap: Characters to overlap between chunks

    Returns:
        List of text chunks
    """
    # Handle trivial cases
    if not text:
        return []
    if len(text) <= chunk_size:
        return [text]

    chunks = []
    start_position = 0
    text_length = len(text)

    while start_position < text_length:
        # Calculate tentative end position
        tentative_end = min(start_position + chunk_size, text_length)

        # Find a natural break point if we're not at the end
        if tentative_end < text_length:
            end_position = _find_sentence_boundary(
                text, start_position, tentative_end, chunk_size
            )
        else:
            end_position = text_length

        current_chunk = text[start_position:end_position]

        # Add the chunk
        cleaned_chunk = current_chunk.strip()
        if cleaned_chunk:
            chunks.append(cleaned_chunk)

        # Calculate next start position with overlap
        next_start = end_position - overlap

        # Prevent infinite loops: ensure forward progress
        if next_start <= start_position:
            next_start = end_position

        start_position = next_start

    # Post-process: merge tiny last chunk with previous one
    chunks = _merge_tiny_chunks(chunks, chunk_size)

    return chunks


def _find_sentence_boundary(
    text: str,
    chunk_start: int,
    tentative_end: int,
    chunk_size: int
) -> int:
    """
    Find the nearest sentence boundary before the tentative end.

    Looks for sentence-ending punctuation or newlines to avoid
    breaking in the middle of a sentence. If no good boundary
    found within the chunk window, extends search forward up to
    chunk_size/2 characters to find one.

    Args:
        text: Full text being chunked
        chunk_start: Start position of current chunk
        tentative_end: Tentative end position
        chunk_size: Target chunk size (used for max extension)

    Returns:
        Adjusted end position at a sentence boundary
    """
    MIN_CHUNK_SIZE = max(30, chunk_size // 4)

    # Extract the current chunk to search within
    search_window = text[chunk_start:tentative_end]

    # Find the last sentence boundary in the chunk
    boundary_matches = list(re.finditer(SENTENCE_BOUNDARIES, search_window))

    if boundary_matches:
        last_match = boundary_matches[-1]
        return chunk_start + last_match.end()

    # No sentence boundary found - extend search forward
    # Look up to chunk_size/2 characters ahead for a boundary
    max_extension = chunk_size // 2
    extended_window = text[chunk_start:tentative_end + max_extension]
    extended_matches = list(re.finditer(SENTENCE_BOUNDARIES, extended_window))

    if extended_matches:
        last_match = extended_matches[-1]
        return chunk_start + last_match.end()

    # Still no boundary - find next newline or paragraph break
    # Search in extended window for paragraph breaks (double newline)
    paragraph_matches = list(re.finditer(r'\n\n+', extended_window))
    if paragraph_matches:
        return chunk_start + paragraph_matches[0].start()

    # Last resort: find any substantial break (single newline with content)
    for i in range(tentative_end - 1, max(chunk_start + MIN_CHUNK_SIZE, tentative_end - 1), -1):
        if text[i] in '\n\r' and (i + 1 < len(text)) and text[i + 1].strip():
            return i + 1

    # Fallback: use tentative_end if nothing else found
    return tentative_end


def _merge_tiny_chunks(chunks: List[str], chunk_size: int) -> List[str]:
    """
    Merge tiny leftover chunks with the previous chunk.

    Args:
        chunks: List of text chunks
        chunk_size: Target chunk size (used for minimum threshold)

    Returns:
        Merged chunks list
    """
    if len(chunks) < 2:
        return chunks

    MIN_TINY = max(30, chunk_size // 4)

    # Check if last chunk is tiny
    if len(chunks[-1]) < MIN_TINY:
        # Merge with previous chunk
        merged = chunks[-2] + '\n' + chunks[-1]
        chunks = chunks[:-2] + [merged]

    return chunks

def _chunk_by_chapter(text: str) -> List[str]:
    """
    Split text by heading structure (markdown or HTML).

    Recognizes:
    - Markdown: ## Heading, ### Subheading
    - HTML: <h2>Heading</h2>, <h3>Subheading</h3>

    Args:
        text: Markdown or HTML text

    Returns:
        List of sections (each starts with its heading)
    """
    if not text:
        return []

    lines = text.split('\n')
    chunks = []
    current_section = []

    for line in lines:
        # Check if this line starts a new section
        if _is_heading_line(line):
            # Save the previous section if it exists
            saved_section = _finalize_section(current_section)
            if saved_section:
                chunks.append(saved_section)

            # Start a new section
            current_section = [line]
        else:
            current_section.append(line)

    # Save the final section
    final_section = _finalize_section(current_section)
    if final_section:
        chunks.append(final_section)

    # If no headings found, return entire text
    return chunks if chunks else [text]


def _is_heading_line(line: str) -> bool:
    """
    Check if a line is a heading (h2 or h3).

    Supports:
    - Markdown: ##, ###
    - HTML: <h2>, <h3>

    Args:
        line: Line of text to check

    Returns:
        True if the line is a heading
    """
    # Markdown headings
    if re.match(r'^#{2,3}\s+', line):
        return True

    # HTML headings
    if re.match(r'^<h[2-3]', line, re.IGNORECASE):
        return True

    return False


def _finalize_section(lines: List[str]) -> Optional[str]:
    """
    Join section lines and clean up whitespace.

    Args:
        lines: Lines belonging to a section

    Returns:
        Cleaned section text, or None if empty
    """
    if not lines:
        return None

    section_text = '\n'.join(lines).strip()
    return section_text if section_text else None


# ──────────────────────── Utilities ────────────────────────

def _truncate_text(text: str, max_length: int) -> str:
    """
    Truncate text to max_length with ellipsis.

    Args:
        text: Text to truncate
        max_length: Maximum characters

    Returns:
        Truncated text with "..." if shortened
    """
    if len(text) <= max_length:
        return text

    return text[:max_length] + "..."


# ─────────────────── Field-based Chunking (Stage1 style) ───────────────────

def clean_html_for_chunking(html: str) -> str:
    """Clean HTML for field extraction. Removes noise, preserves structure."""
    from html.parser import HTMLParser

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

    # Clean up
    text = re.sub(r'[A-Za-z0-9+/]{80,}={0,2}', '', text)
    text = re.sub(r'data:image[^\s]+', '', text)
    text = re.sub(r'\s+', ' ', text)

    return text.strip()


FIELD_EXTRACT_SYSTEM = """你是一个医疗病历字段提取助手。你的任务是从病历文本中提取所有字段名和字段值。

【规则】
1. 不遗漏任何字段 - 文本中有什么就提取什么
2. 不编造字段 - 只提取真实存在的
3. 保持字段名和字段值不变，不要翻译或修改
4. 空值填 ""，保持原文
5. 直接输出JSON数组，不要用markdown代码块包裹

【输出格式】
[{"field":"字段名","value":"字段值"},...]"""


FIELD_EXTRACT_USER = """请从下面病历中提取所有字段：

{cleaned_text}

直接输出JSON数组，不要其他内容："""


def llm_extract_fields(html_text: str, client) -> List[Dict[str, str]]:
    """
    Use LLM to extract fields from HTML text.

    Args:
        html_text: Raw or cleaned HTML text
        client: OllamaClient instance

    Returns:
        List of {"field": "...", "value": "..."} dicts
    """
    cleaned = clean_html_for_chunking(html_text)
    user_prompt = FIELD_EXTRACT_USER.format(cleaned_text=cleaned[:6000])

    try:
        response = client.chat(
            messages=[
                {"role": "system", "content": FIELD_EXTRACT_SYSTEM},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.0
        )

        # Parse JSON response
        import json
        # Try to extract JSON from response
        text = response.strip()
        if "```json" in text:
            parts = text.split("```json")
            if len(parts) >= 2:
                text = parts[1].split("```")[0]
        elif "```" in text:
            parts = text.split("```")
            if len(parts) >= 2:
                text = parts[1]

        fields = json.loads(text)
        return fields
    except Exception as e:
        # Fallback: return empty
        return []


# Semantic field groups for common medical sections
FIELD_SECTION_KEYWORDS = {
    "基础信息": ["姓名", "性别", "年龄", "出生", "民族", "职业", "婚姻", "病史陈述", "记录时间", "入院时间", "科室", "床号", "病案号"],
    "主诉": ["主诉"],
    "现病史": ["现病史"],
    "既往史": ["既往史", "个人史", "家族史", "婚姻史", "月经生育史"],
    "体格检查": ["体格检查", "一般情况", "皮肤", "头颅", "颈部", "胸部", "肺部", "心脏", "腹部", "四肢", "神经"],
    "辅助检查": ["辅助检查", "实验室检查", "影像学检查", "CT", "MR", "X线", "超声"],
    "诊断": ["诊断", "初步诊断", "确定诊断"],
    "治疗": ["治疗", "处置", "手术", "用药", "医嘱"],
    "知情同意": ["知情", "同意", "委托", "授权"],
}

# Keywords that indicate section headers (not field values)
SECTION_HEADER_KEYWORDS = [
    "入院记录", "病程记录", "术前小结", "出院记录",
    "查房记录", "会诊意见", "知情同意书", "检查报告"
]


def group_fields_by_semantic(fields: List[Dict[str, str]]) -> Dict[str, Dict[str, str]]:
    """
    Group extracted fields by semantic section.

    Args:
        fields: List of {"field": "...", "value": "..."} dicts

    Returns:
        Dict of section_name -> {field: value, ...}
    """
    groups = {}

    current_section = "其他"
    current_fields = {}

    for item in fields:
        field_name = item.get("field", "")
        field_value = item.get("value", "")

        if not field_name or not field_value:
            continue

        # Check if this field is a section header
        is_header = False
        detected_section = None

        # Check exact section keywords
        for section, keywords in FIELD_SECTION_KEYWORDS.items():
            for kw in keywords:
                if kw in field_name and len(field_name) < 10:
                    is_header = True
                    detected_section = section
                    break
            if is_header:
                break

        # Check document type headers
        for header in SECTION_HEADER_KEYWORDS:
            if header in field_name:
                is_header = True
                detected_section = "文档信息"
                break

        if is_header and detected_section:
            # Save previous section if has fields
            if current_fields:
                groups[current_section] = current_fields

            current_section = detected_section
            current_fields = {field_name: field_value}
        else:
            # Add to current section
            current_fields[field_name] = field_value

    # Save last section
    if current_fields:
        groups[current_section] = current_fields

    return groups


def chunk_by_fields(html_text: str, client) -> List[str]:
    """
    Chunk HTML by extracted fields using LLM.

    Process:
    1. clean_html() - remove noise from HTML
    2. LLM extract fields (JSON)
    3. Group fields by semantic section
    4. Each group -> one chunk with ## heading

    Args:
        html_text: Raw HTML text
        client: OllamaClient instance

    Returns:
        List of chunk strings
    """
    # Extract fields using LLM
    fields = llm_extract_fields(html_text, client)

    if not fields:
        # Fallback to chapter chunking
        cleaned = clean_html_for_chunking(html_text)
        return _chunk_by_chapter(cleaned)

    # Group fields by semantic section
    groups = group_fields_by_semantic(fields)

    # Convert each group to a chunk
    chunks = []
    for section_name, section_fields in groups.items():
        chunk_text = f"## {section_name}\n"
        for field, value in section_fields.items():
            chunk_text += f"- {field}：{value}\n"
        chunks.append(chunk_text.strip())

    return chunks if chunks else []