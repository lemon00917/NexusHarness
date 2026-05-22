"""
Document Parser for NexusHarness RAG
==================================
Supports HTML, PDF, MD, TXT file parsing.
"""

import re
from pathlib import Path
from typing import Optional


def strip_html(text: str) -> str:
    """Remove HTML tags, CSS, and scripts from text."""
    # Remove script and style elements with their content
    text = re.sub(r'<script[^>]*>[\s\S]*?</script>', ' ', text)
    text = re.sub(r'<style[^>]*>[\s\S]*?</style>', ' ', text)
    # Remove all HTML tags
    text = re.sub(r'<[^>]+>', ' ', text)
    # Decode common HTML entities
    text = _decode_html_entities(text)
    # Remove extra whitespace
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def _decode_html_entities(text: str) -> str:
    """Decode HTML entities to characters."""
    entities = {
        '&nbsp;': ' ', '&nbsp': ' ',
        '&lt;': '<', '&gt;': '>',
        '&amp;': '&', '&quot;': '"',
        '&#39;': "'", '&apos;': "'",
        '&ndash;': '-', '&mdash;': '-',
        '&copy;': '(c)', '&reg;': '(R)',
        '&trade;': '(TM)',
    }
    for entity, char in entities.items():
        text = text.replace(entity, char)
    # Handle numeric entities &#123;
    text = re.sub(r'&#(\d+);', lambda m: chr(int(m.group(1))), text)
    text = re.sub(r'&#x([0-9a-fA-F]+);', lambda m: chr(int(m.group(1), 16)), text)
    return text


def parse_html(content: str) -> str:
    """Parse HTML document, removing tags and extracting readable text."""
    return strip_html(content)


def parse_pdf(content: bytes, filename: str = "") -> str:
    """Parse PDF document, extracting text content."""
    try:
        from pypdf import PdfReader
        from io import BytesIO

        reader = PdfReader(BytesIO(content))
        text_parts = []

        for page in reader.pages:
            page_text = page.extract_text()
            if page_text:
                text_parts.append(page_text)

        return '\n\n'.join(text_parts)
    except Exception as e:
        return f"[PDF 解析失败: {filename} - {e}]"


def parse_markdown(content: str) -> str:
    """Parse Markdown document (mostly passthrough, just clean up)."""
    # Remove image syntax
    text = re.sub(r'!\[.*?\]\(.*?\)', '', content)
    # Remove link syntax but keep text
    text = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', text)
    # Remove emphasis markers
    text = re.sub(r'[*_]{1,2}([^*_]+)[*_]{1,2}', r'\1', text)
    # Remove headers markers
    text = re.sub(r'^#+\s+', '', text, flags=re.MULTILINE)
    # Remove horizontal rules
    text = re.sub(r'^[-*_]{3,}$', '', text, flags=re.MULTILINE)
    # Clean up whitespace
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def parse_text(content: str) -> str:
    """Parse plain text (passthrough)."""
    return content.strip()


def parse_document(content: bytes, filename: str, encoding: str = "utf-8") -> str:
    """
    Parse document based on file extension.

    Args:
        content: Raw file content as bytes
        filename: Original filename
        encoding: Text encoding to use (default utf-8)

    Returns:
        Parsed text content
    """
    filename_lower = filename.lower()

    # Decode bytes to text
    try:
        text = content.decode(encoding)
    except UnicodeDecodeError:
        try:
            text = content.decode("gbk")
        except UnicodeDecodeError:
            text = content.decode(encoding, errors="replace")

    # Parse based on extension
    if filename_lower.endswith(('.html', '.htm')):
        return parse_html(text)
    elif filename_lower.endswith('.pdf'):
        return parse_pdf(content, filename)
    elif filename_lower.endswith('.md'):
        return parse_markdown(text)
    elif filename_lower.endswith(('.txt', '.text')):
        return parse_text(text)
    elif filename_lower.endswith('.json'):
        try:
            import json
            data = json.loads(text)
            return json.dumps(data, ensure_ascii=False, indent=2)
        except Exception:
            return parse_text(text)
    else:
        # Default: treat as text
        return parse_text(text)