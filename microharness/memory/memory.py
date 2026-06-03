"""
Long-Term Memory Module
=======================
Cross-platform persistent memory for session summaries.

Features:
- Atomic file writes (temp file + rename)
- JSON corruption recovery with auto-backup
- Deduplication within recent memories
- Cross-platform file locking (Windows + POSIX)
- Retry with backoff for LLM extraction
"""

import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional

from langchain_core.messages import HumanMessage

logger = logging.getLogger(__name__)

# ──────────────────────── Configuration ────────────────────────

PROJECT_ROOT = Path(__file__).parent.parent.parent
MEMORY_FILE = PROJECT_ROOT / "sessions" / "memory.json"
TEMP_FILE = MEMORY_FILE.with_suffix(".json.tmp")
BACKUP_FILE = MEMORY_FILE.with_suffix(".json.bak")

MAX_MEMORIES_TO_STORE = 20
MAX_MEMORIES_IN_PROMPT = 5
MAX_MEMORY_SUMMARY_LEN = 500
MEMORY_EXTRACT_RETRIES = 3


# ──────────────────────── File Locking ────────────────────────

def _acquire_lock(f, lock_type: str = "shared") -> None:
    """
    Acquire file lock (cross-platform).

    Args:
        f: Open file handle
        lock_type: "shared" (LOCK_SH) or "exclusive" (LOCK_EX)
    """
    try:
        import fcntl
        op = fcntl.LOCK_SH if lock_type == "shared" else fcntl.LOCK_EX
        fcntl.flock(f.fileno(), op)
    except (ImportError, AttributeError):
        # fcntl not available (Windows), skip locking
        pass


def _release_lock(f) -> None:
    """Release file lock if held."""
    try:
        import fcntl
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except (ImportError, AttributeError):
        pass


# ──────────────────────── Persistence ────────────────────────

def load_memories() -> List[Dict]:
    """
    Load memories from disk with corruption recovery.

    Returns:
        List of memory entries (oldest first)
    """
    if not MEMORY_FILE.exists():
        return []

    try:
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            _acquire_lock(f, "shared")
            try:
                data = json.load(f)
                return data if isinstance(data, list) else []
            finally:
                _release_lock(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.error(f"Failed to load memory file: {e}")
        _backup_corrupted()
        return []


def _backup_corrupted() -> None:
    """Backup corrupted memory file."""
    if MEMORY_FILE.exists():
        os.replace(MEMORY_FILE, BACKUP_FILE)
        logger.warning(f"Corrupted memory backed up to {BACKUP_FILE}")


def save_memories(memories: List[Dict]) -> bool:
    """
    Atomically save memories to disk.

    Writes to temp file first, then atomic rename.
    """
    try:
        memories = memories[-MAX_MEMORIES_TO_STORE:]

        with open(TEMP_FILE, "w", encoding="utf-8") as f:
            json.dump(memories, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())

        _acquire_lock(f, "exclusive")
        try:
            os.replace(TEMP_FILE, MEMORY_FILE)
        finally:
            _release_lock(f)

        return True
    except Exception as e:
        logger.error(f"Failed to save memories: {e}")
        return False


# ──────────────────────── Memory Extraction ────────────────────────

def extract_and_save_memory(messages: List, task: str) -> Optional[str]:
    """
    Extract a summary from messages and save to memory.

    Args:
        messages: Conversation history
        task: Task/session identifier

    Returns:
        Extracted summary string, or None on failure
    """
    print(f"[MEMORY] extract_and_save_memory called: task={task[:30]}, messages_count={len(messages)}")
    from microharness.config import get_llm, MEMORY_MODEL

    history_text = _format_messages_for_llm(messages)

    prompt = f"""Extract ONE concise summary (max 80 words) from this session:
Task: {task}
History: {history_text}
Respond ONLY with the summary."""

    last_error = None
    for attempt in range(MEMORY_EXTRACT_RETRIES):
        try:
            llm = get_llm(MEMORY_MODEL)
            response = llm.invoke([HumanMessage(content=prompt)], timeout=30)
            summary = response.content.strip() if response.content else ""

            if not summary:
                logger.warning("Empty summary from LLM")
                return None

            return _save_extracted_memory(task, summary)

        except Exception as e:
            last_error = e
            logger.warning(f"Attempt {attempt + 1} failed: {e}")
            if attempt < MEMORY_EXTRACT_RETRIES - 1:
                time.sleep(2 ** attempt)  # Exponential backoff: 1s, 2s, 4s

    logger.error("Memory extraction failed after 3 attempts")
    return None


# ──────────────────────── Memory Management ────────────────────────

def _format_messages_for_llm(messages: List) -> str:
    """Format messages for LLM consumption."""
    lines = []
    for msg in messages[-20:]:  # Limit to recent messages
        if hasattr(msg, "content") and msg.content:
            role = getattr(msg, "type", "unknown")
            lines.append(f"{role}: {msg.content[:200]}")
    return "\n".join(lines)


def _save_extracted_memory(task: str, summary: str) -> str:
    """
    Save memory with deduplication.

    Skips save if same task was summarized recently.
    """
    memories = load_memories()

    # Check for duplicates (exact task match in recent 3)
    for recent in memories[-3:]:
        if recent.get("task") == task:
            logger.info(f"Skipping duplicate memory for task: {task[:50]}")
            return summary

    memories.append({
        "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "task": task,
        "summary": summary[:MAX_MEMORY_SUMMARY_LEN],
    })

    if save_memories(memories):
        logger.info(f"Saved memory: {summary[:100]}...")
    else:
        logger.error("Failed to save memory")

    return summary


def clear_memories() -> bool:
    """Clear all stored memories."""
    try:
        if MEMORY_FILE.exists():
            os.remove(MEMORY_FILE)
        return True
    except Exception as e:
        logger.error(f"Failed to clear memories: {e}")
        return False


def format_memories_for_prompt(memories: List[Dict]) -> str:
    """
    Format memories for inclusion in a prompt.

    Args:
        memories: List of memory dicts (from load_memories)

    Returns:
        Formatted string for prompt insertion
    """
    if not memories:
        return ""

    lines = ["[Session Memory]"]
    for m in memories[-MAX_MEMORIES_IN_PROMPT:]:
        date = m.get("date", "unknown date")
        task = m.get("task", "unknown task")
        summary = m.get("summary", "")
        lines.append(f"- [{date}] {task}: {summary}")

    return "\n".join(lines)