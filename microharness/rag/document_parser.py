"""
Document Parser for NexusHarness RAG
====================================
Multi-format document parser supporting HTML, PDF, Markdown, TXT, and JSON.
"""

import json
import re
from io import BytesIO
from pathlib import Path
from dataclasses import dataclass
from typing import Dict, Callable, Optional


# ──────────────────────── Parser Marker ────────────────────────

@dataclass
class ParserConfig:
    """Marker for parser registration with metadata."""
    extensions: tuple
    binary: bool = False


# ──────────────────────── Configuration ────────────────────────

# HTML entities mapping for decoding
HTML_ENTITIES = {
    '&nbsp;': ' ', '&nbsp': ' ',
    '&lt;': '<', '&gt;': '>',
    '&amp;': '&', '&quot;': '"',
    '&#39;': "'", '&apos;': "'",
    '&ndash;': '-', '&mdash;': '—',
    '&copy;': '©', '&reg;': '®',
    '&trade;': '™',
    '&ldquo;': '"', '&rdquo;': '"',
    '&lsquo;': "'", '&rsquo;': "'",
}

# File extension to parser mapping
PARSER_REGISTRY: Dict[str, Callable] = {}

# Fallback encodings to try
FALLBACK_ENCODINGS = ['utf-8', 'gbk', 'latin-1']


# ──────────────────────── Parser Decorator ────────────────────────

def register_parser(*extensions: str, binary: bool = False):
    """
    Decorator to register a parser function for specific file extensions.

    Args:
        binary: Whether the parser expects raw bytes (True) or decoded text (False)

    Usage:
        @register_parser('.html', '.htm')
        def parse_html(content): ...

        @register_parser('.pdf', binary=True)
        def parse_pdf(content): ...
    """
    def decorator(func: Callable):
        for ext in extensions:
            PARSER_REGISTRY[ext.lower()] = func
        func._parser_config = ParserConfig(extensions=extensions, binary=binary)
        return func
    return decorator


# ──────────────────────── Core Parsing ────────────────────────

def parse_document(
    content: bytes,
    filename: str,
    encoding: str = "utf-8"
) -> str:
    """
    Parse a document based on its file extension.

    Supports: HTML, PDF, Markdown, TXT, JSON

    Args:
        content: Raw file content as bytes
        filename: Original filename (used to detect format)
        encoding: Preferred text encoding (default: utf-8)

    Returns:
        Parsed and cleaned text content

    Raises:
        ValueError: If the file format is unsupported
    """
    file_extension = _get_extension(filename)
    parser = PARSER_REGISTRY.get(file_extension)

    if parser is None:
        raise ValueError(
            f"Unsupported file format: '{file_extension}'. "
            f"Supported: {list(PARSER_REGISTRY.keys())}"
        )

    # Determine if parser needs text or bytes
    parser_config = getattr(parser, '_parser_config', ParserConfig(extensions=(), binary=False))

    if parser_config.binary:
        return parser(content, filename)
    else:
        text = _decode_bytes(content, preferred_encoding=encoding)
        return parser(text)


# ──────────────────────── Format Parsers ────────────────────────

@register_parser('.html', '.htm')
def parse_html(content: str) -> str:
    """
    Parse HTML document, preserving h2/h3 headings for chapter chunking.

    Process:
    1. Remove invisible elements (script, style, head)
    2. Convert h2/h3 to markdown headings (for chapter chunking)
    3. Strip remaining HTML tags
    4. Decode HTML entities
    5. Normalize whitespace
    """
    if not content:
        return ""

    # Remove invisible elements
    content = _remove_html_elements(content, 'script')
    content = _remove_html_elements(content, 'style')
    content = _remove_html_elements(content, 'head')

    # Convert h2/h3 to markdown headings (preserve for chapter chunking)
    content = re.sub(r'<h2[^>]*>(.*?)</h2>', r'\n## \1\n', content, flags=re.IGNORECASE | re.DOTALL)
    content = re.sub(r'<h3[^>]*>(.*?)</h3>', r'\n### \1\n', content, flags=re.IGNORECASE | re.DOTALL)

    # Strip remaining HTML tags
    content = re.sub(r'<[^>]+>', ' ', content)

    # Decode HTML entities
    content = _decode_html_entities(content)

    # Normalize whitespace (but preserve newlines from heading conversion)
    content = re.sub(r'[ \t]+', ' ', content)  # Only normalize spaces/tabs, not newlines
    content = re.sub(r'\n\n+', '\n\n', content)  # Collapse multiple newlines
    return content.strip()


@register_parser('.pdf', binary=True)
def parse_pdf(content: bytes, filename: str = "") -> str:
    """
    Extract text from PDF documents.

    Args:
        content: Raw PDF bytes
        filename: Original filename (for error messages)

    Returns:
        Extracted text or error message
    """
    try:
        from pypdf import PdfReader

        reader = PdfReader(BytesIO(content))
        text_pages = []

        for page_num, page in enumerate(reader.pages, start=1):
            page_text = page.extract_text()
            if page_text:
                text_pages.append(page_text)

        if not text_pages:
            return f"[PDF 无文本内容: {filename}]"

        # Use page separator unlikely to appear in content
        return '\n\n--- Page Break ---\n\n'.join(text_pages)

    except ImportError:
        return f"[PDF 解析失败: 未安装 pypdf 库。请执行: pip install pypdf]"
    except Exception as e:
        return f"[PDF 解析失败: {filename} - {type(e).__name__}: {e}]"


@register_parser('.md', '.markdown')
def parse_markdown(content: str) -> str:
    """
    Clean markdown text by removing formatting syntax.

    Preserves: paragraph structure, list items
    Removes: links, images, emphasis markers, headers
    """
    if not content:
        return ""

    # Remove images: ![alt](url)
    content = re.sub(r'!\[.*?\]\(.*?\)', '', content)

    # Convert links to text: [text](url) -> text
    content = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', content)

    # Remove emphasis: *italic*, **bold**
    content = re.sub(r'\*{1,2}([^*]+)\*{1,2}', r'\1', content)
    content = re.sub(r'_{1,2}([^_]+)_{1,2}', r'\1', content)

    # Strip header markers: ### Title -> Title
    content = re.sub(r'^#{1,6}\s+', '', content, flags=re.MULTILINE)

    # Remove horizontal rules: ---, ***, ___
    content = re.sub(r'^[-*_]{3,}\s*$', '', content, flags=re.MULTILINE)

    # Normalize blank lines (max 2 consecutive newlines)
    content = re.sub(r'\n{3,}', '\n\n', content)

    return content.strip()


@register_parser('.txt', '.text')
def parse_text(content: str) -> str:
    """Parse plain text (passthrough with whitespace normalization)."""
    if not content:
        return ""
    return content.strip()


@register_parser('.json')
def parse_json(content: str) -> str:
    """
    Parse JSON files, pretty-printing the content.

    Returns:
        Formatted JSON string or original content with normalized whitespace if parsing fails
    """
    try:
        data = json.loads(content)
        return json.dumps(data, ensure_ascii=False, indent=2)
    except json.JSONDecodeError:
        # Return with normalized whitespace, preserve structure
        return re.sub(r'\s+', ' ', content).strip()


# ──────────────────────── HTML Processing Helpers ────────────────────────

def _remove_html_elements(text: str, tag: str) -> str:
    """
    Remove HTML elements and their content.

    Args:
        text: HTML text
        tag: Tag name to remove (e.g., 'script', 'style')

    Returns:
        Text with specified elements removed
    """
    pattern = rf'<{tag}[^>]*>[\s\S]*?</{tag}>'
    return re.sub(pattern, ' ', text, flags=re.IGNORECASE)


def _decode_html_entities(text: str) -> str:
    """
    Decode HTML entities to their character equivalents.

    Handles:
    - Named entities (&amp;, &lt;, etc.)
    - Numeric entities (&#123;, &#x1F600;)

    Args:
        text: Text containing HTML entities

    Returns:
        Text with entities decoded
    """
    # Decode named entities
    for entity, char in HTML_ENTITIES.items():
        text = text.replace(entity, char)

    # Decode decimal numeric entities: &#123;
    text = re.sub(
        r'&#(\d+);',
        lambda m: chr(int(m.group(1))),
        text
    )

    # Decode hex numeric entities: &#x1F600;
    text = re.sub(
        r'&#x([0-9a-fA-F]+);',
        lambda m: chr(int(m.group(1), 16)),
        text
    )

    return text


# ──────────────────────── Utility Functions ────────────────────────

def _get_extension(filename: str) -> str:
    """
    Extract file extension in lowercase.

    Args:
        filename: File name or path

    Returns:
        Lowercase extension with dot (e.g., '.pdf')
    """
    return Path(filename).suffix.lower()


def _decode_bytes(
    content: bytes,
    preferred_encoding: str = "utf-8"
) -> str:
    """
    Decode bytes to string, trying multiple encodings.

    Args:
        content: Raw bytes
        preferred_encoding: Encoding to try first

    Returns:
        Decoded string (uses replacement chars for un-decodable bytes)
    """
    # Try preferred encoding first, then fallbacks
    encodings_to_try = [preferred_encoding] + [
        enc for enc in FALLBACK_ENCODINGS
        if enc != preferred_encoding
    ]

    for encoding in encodings_to_try:
        try:
            return content.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue

    # Last resort: decode with replacement
    return content.decode(preferred_encoding, errors="replace")


def get_supported_formats() -> Dict[str, str]:
    """
    Get mapping of supported file extensions to parser descriptions.

    Returns:
        Dictionary of {extension: description}
    """
    format_descriptions = {
        '.html': 'HTML documents',
        '.htm': 'HTML documents',
        '.pdf': 'PDF documents',
        '.md': 'Markdown files',
        '.markdown': 'Markdown files',
        '.txt': 'Plain text files',
        '.text': 'Plain text files',
        '.json': 'JSON data files',
    }

    return {
        ext: desc
        for ext, desc in format_descriptions.items()
        if ext in PARSER_REGISTRY
    }