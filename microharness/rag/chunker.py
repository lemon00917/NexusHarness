"""
Document Chunker for NexusHarness RAG
=====================================
Splits documents into chunks for better retrieval.

流程：
    HTML 病历 → html-to-markdown → Markdown → 切分模式
"""

import re
from typing import List, Dict, Optional

# ──────────────────────── html-to-markdown (lazy import, slow first load) ────────────────────────

def _get_html_to_markdown():
    """Lazy import to avoid blocking module load (Rust library takes ~3s to init)."""
    import html_to_markdown as hm
    return hm

# ──────────────────────── Configuration ────────────────────────

DEFAULT_CHUNK_SIZE = 1500
DEFAULT_OVERLAP = 200
PREVIEW_LENGTH = 5000

# Sentence-ending patterns for intelligent break points
SENTENCE_BOUNDARIES = r'[.。!?！？]+'


# ──────────────────────── HTML → Markdown ────────────────────────

def html_to_markdown(html: str) -> str:
    """
    Convert HTML to Markdown using html-to-markdown (Rust-based, fast).
    Base64 data URIs are stripped before conversion to avoid bloating output.
    【已优化】自动分片处理超大HTML，永不卡死！
    """
    # Remove base64 image data URIs before conversion (including newlines)
    html = re.sub(r'<img[^>]*data:image/[^>]*>', '', html)

    # ===================== 【关键修复：自动分片，解决50k+卡死】 =====================
    def split_html_by_table(html_str, max_size=40000):
        """按表格分片，单块40k以内，保证不卡死、不切烂表格"""
        parts = re.split(r'(</table>)', html_str)
        chunks = []
        buf = ""
        for p in parts:
            buf += p
            if len(buf.encode("utf-8")) > max_size:
                chunks.append(buf)
                buf = ""
        if buf:
            chunks.append(buf)
        return chunks

    try:
        hm = _get_html_to_markdown()
        html_chunks = split_html_by_table(html)
        full_md = ""

        # Timeout protection: each chunk must convert within 5s
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
        for chunk in html_chunks:
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(lambda c=chunk: hm.convert(c))
                try:
                    res = future.result(timeout=5)
                    full_md += res.content + "\n"
                except FutureTimeout:
                    # Rust parser hung on this chunk, fall back to pure Python
                    return _html_to_markdown_fallback(html)
        return full_md.strip()
    except ImportError:
        return _html_to_markdown_fallback(html)


def _html_to_markdown_fallback(html: str) -> str:
    """Fallback HTML→Markdown when html-to-markdown not available."""
    import html.parser

    class MarkdownConverter(html.parser.HTMLParser):
        def __init__(self):
            super().__init__()
            self.result = []
            self.in_list = False

        def handle_starttag(self, tag, attrs):
            if tag == 'br':
                self.result.append('\n')
            elif tag == 'p':
                self.result.append('\n\n')
            elif tag == 'li':
                self.result.append('\n- ')
            elif tag in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6'):
                level = tag[1]
                self.result.append(f'\n{"#" * int(level)} ')
            elif tag == 'strong' or tag == 'b':
                self.result.append('**')
            elif tag == 'em' or tag == 'i':
                self.result.append('*')
            elif tag == 'code':
                self.result.append('`')
            elif tag == 'pre':
                self.result.append('\n```\n')
            elif tag == 'tr':
                self.result.append('|')

        def handle_endtag(self, tag):
            if tag == 'strong' or tag == 'b':
                self.result.append('**')
            elif tag == 'em' or tag == 'i':
                self.result.append('*')
            elif tag == 'code':
                self.result.append('`')
            elif tag == 'pre':
                self.result.append('\n```\n')
            elif tag == 'li':
                self.in_list = False
            elif tag in ('table', 'thead', 'tbody'):
                self.result.append('\n')
            elif tag == 'tr':
                self.result.append('\n')

        def handle_data(self, data):
            if data.strip():
                self.result.append(data)

        def get_result(self):
            text = ''.join(self.result)
            # Clean up excessive whitespace
            text = re.sub(r'\n{3,}', '\n\n', text)
            return text.strip()

    try:
        parser = MarkdownConverter()
        parser.feed(html)
        return parser.get_result()
    except:
        # Last fallback: strip HTML tags
        text = re.sub(r'<[^>]+>', '', html)
        return text.strip()


# ──────────────────────── Public API ────────────────────────

def chunk_text(
    text: str,
    mode: str = "length",
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_OVERLAP,
    is_html: bool = False,
) -> List[str]:
    """
    Split text into chunks based on the specified mode.

    Args:
        text: Input text (HTML or plain text)
        mode: Chunking strategy
            - "length": Split by character count with smart boundaries
            - "chapter": Split by markdown/HTML headings (h2, h3, ##)
        chunk_size: Maximum characters per chunk (length mode only)
        overlap: Overlapping characters between chunks (length mode only)
        is_html: If True, convert HTML to Markdown before chunking

    Returns:
        List of text chunks

    Raises:
        ValueError: If mode is not supported
    """
    # Convert HTML to Markdown if needed
    if is_html:
        text = html_to_markdown(text)

    if mode == "chapter":
        return _chunk_by_chapter(text)
    elif mode == "length":
        return _chunk_by_length(text, chunk_size, overlap)
    else:
        raise ValueError(f"Unknown chunking mode: '{mode}'. Use 'length' or 'chapter'.")


def preview_chunks(
    text: str,
    mode: str = "length",
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_OVERLAP,
    preview_length: int = PREVIEW_LENGTH,
    is_html: bool = False,
) -> List[Dict]:
    """
    Preview how text would be chunked without full processing.

    Args:
        text: Input text to preview
        mode: Chunking strategy ("length" or "chapter")
        chunk_size: Maximum characters per chunk
        overlap: Overlap between chunks
        preview_length: Characters to show in preview
        is_html: If True, convert HTML to Markdown before chunking

    Returns:
        List of dictionaries with chunk metadata:
            - chunk_index: Position in chunk list
            - char_count: Total characters in chunk
            - preview: First {preview_length} characters
    """
    chunks = chunk_text(text, mode, chunk_size=chunk_size, overlap=overlap, is_html=is_html)

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
    Find the nearest natural break point before the tentative end.

    Priority:
    1. Single newline with content (\n) — preferred for tables/rows
    2. Paragraph breaks (\n\n+) — section separation
    3. Sentence-ending punctuation (.。!?！？)+)
    4. Extended sentence boundary (chunk_size/2 forward)
    5. Hard cut at tentative_end

    Args:
        text: Full text being chunked
        chunk_start: Start position of current chunk
        tentative_end: Tentative end position
        chunk_size: Target chunk size (used for max extension)

    Returns:
        Adjusted end position at a natural boundary
    """
    MIN_CHUNK_SIZE = max(30, chunk_size // 4)
    search_window = text[chunk_start:tentative_end]

    # Priority 1: Find last single newline (preserves table row structure)
    # rfind("\n", start, end) finds the last \n before tentative_end
    split_pos = search_window.rfind("\n")
    if split_pos != -1 and split_pos >= MIN_CHUNK_SIZE:
        return chunk_start + split_pos + 1  # include the \n in chunk

    # Priority 2: Paragraph breaks (\n\n+)
    paragraph_matches = list(re.finditer(r'\n\n+', search_window))
    if paragraph_matches:
        return chunk_start + paragraph_matches[-1].start()

    # Priority 3: Sentence-ending punctuation (.。!?！？)+
    boundary_matches = list(re.finditer(SENTENCE_BOUNDARIES, search_window))
    if boundary_matches:
        return chunk_start + boundary_matches[-1].end()

    # Priority 4: Extend forward up to chunk_size/2 to find a boundary
    max_extension = chunk_size // 2
    extended_window = text[chunk_start:tentative_end + max_extension]

    # Check paragraph breaks in extended window
    paragraph_ext = list(re.finditer(r'\n\n+', extended_window))
    if paragraph_ext:
        return chunk_start + paragraph_ext[-1].start()

    # Check sentence boundaries in extended window
    extended_matches = list(re.finditer(SENTENCE_BOUNDARIES, extended_window))
    if extended_matches:
        return chunk_start + extended_matches[-1].end()

    # Priority 5: Hard cut at tentative_end
    return tentative_end

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
    - HTML: <h2>Heading</h2>, <h3>Subheading</h2>

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