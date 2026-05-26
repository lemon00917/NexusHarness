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
    else:
        raise ValueError(f"Unknown chunking mode: '{mode}'. Use 'length' or 'chapter'.")


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
        end_position = min(start_position + chunk_size, text_length)
        current_chunk = text[start_position:end_position]

        # Find a natural break point if we're not at the end
        if end_position < text_length:
            end_position = _find_sentence_boundary(
                text, start_position, end_position
            )
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

    return chunks


def _find_sentence_boundary(
    text: str,
    chunk_start: int,
    tentative_end: int
) -> int:
    """
    Find the nearest sentence boundary before the tentative end.

    Looks for sentence-ending punctuation or newlines to avoid
    breaking in the middle of a sentence.

    Args:
        text: Full text being chunked
        chunk_start: Start position of current chunk
        tentative_end: Tentative end position

    Returns:
        Adjusted end position at a sentence boundary
    """
    # Extract the current chunk to search within
    search_window = text[chunk_start:tentative_end]

    # Find the last sentence boundary in the chunk
    boundary_matches = list(re.finditer(SENTENCE_BOUNDARIES, search_window))

    if boundary_matches:
        # Return position after the last boundary
        last_match = boundary_matches[-1]
        return chunk_start + last_match.end()

    # Fallback: use tentative end if no natural boundary found
    return tentative_end


# ──────────────────────── Chapter-based Chunking ────────────────────────

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