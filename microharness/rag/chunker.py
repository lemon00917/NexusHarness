"""
Document Chunker for NexusHarness RAG
=====================================
Splits documents into chunks for better retrieval.
"""

import re
from typing import List, Tuple


def chunk_by_length(text: str, chunk_size: int = 1500, overlap: int = 200) -> List[str]:
    """
    Split text into chunks by character length with overlap.

    Args:
        text: Input text to chunk
        chunk_size: Maximum characters per chunk
        overlap: Number of overlapping characters between chunks

    Returns:
        List of text chunks
    """
    if not text or len(text) <= chunk_size:
        return [text] if text else []

    chunks = []
    start = 0
    text_len = len(text)

    while start < text_len:
        end = start + chunk_size
        chunk = text[start:end]

        # Try to break at sentence or paragraph boundary
        if end < text_len:
            # Look for sentence breaks
            sentence_break = re.search(r'[.。!?！？\n]+(?!$)', chunk)
            if sentence_break:
                end = start + sentence_break.end()
                chunk = text[start:end]

        chunks.append(chunk.strip())
        start = end - overlap

        # Ensure progress
        if start <= chunks[-1].__len__() - overlap:
            start = end

    return [c for c in chunks if c]


def chunk_by_chapter(text: str) -> List[str]:
    """
    Split text into chunks by markdown or html heading structure.

    Args:
        text: Input text (markdown or html)

    Returns:
        List of text chunks grouped by sections
    """
    if not text:
        return []

    # Pattern for markdown headings (## or ###)
    md_pattern = r'^#{2,3}\s+.+$'

    # Pattern for html headings
    html_pattern = r'<h[2-3][^>]*>.*?</h[2-3]>'

    # Find all heading lines
    lines = text.split('\n')
    chunks = []
    current_chunk_lines = []
    current_heading = ""

    for line in lines:
        md_match = re.match(md_pattern, line, re.MULTILINE)
        html_match = re.search(r'<h[2-3][^>]*>(.*?)</h[2-3]>', line, re.DOTALL)

        if md_match or html_match:
            # Save current chunk if it exists
            if current_chunk_lines:
                chunk_text = '\n'.join(current_chunk_lines)
                if chunk_text.strip():
                    chunks.append(chunk_text.strip())
                current_chunk_lines = []

            heading = html_match.group(1) if html_match else line
            current_heading = heading.strip()

        current_chunk_lines.append(line)

    # Don't forget the last chunk
    if current_chunk_lines:
        chunk_text = '\n'.join(current_chunk_lines)
        if chunk_text.strip():
            chunks.append(chunk_text.strip())

    # If no headings found, return whole text as single chunk
    if not chunks:
        return [text] if text.strip() else []

    return chunks


def chunk_text(text: str, mode: str = "length", **kwargs) -> List[str]:
    """
    Chunk text based on specified mode.

    Args:
        text: Input text to chunk
        mode: "length" or "chapter"
        **kwargs: Additional arguments:
            - chunk_size: for length mode
            - overlap: for length mode

    Returns:
        List of text chunks
    """
    if mode == "chapter":
        return chunk_by_chapter(text)
    else:
        chunk_size = kwargs.get("chunk_size", 1500)
        overlap = kwargs.get("overlap", 200)
        return chunk_by_length(text, chunk_size, overlap)


def preview_chunks(text: str, mode: str = "length", chunk_size: int = 1500,
                  overlap: int = 200) -> List[dict]:
    """
    Preview how text would be chunked without adding to RAG.

    Args:
        text: Input text to preview
        mode: "length" or "chapter"
        chunk_size: for length mode
        overlap: for length mode

    Returns:
        List of chunk info dicts with preview text
    """
    chunks = chunk_text(text, mode, chunk_size=chunk_size, overlap=overlap)

    result = []
    for i, chunk in enumerate(chunks):
        result.append({
            "chunk_index": i,
            "char_count": len(chunk),
            "preview": chunk[:200] + "..." if len(chunk) > 200 else chunk
        })

    return result