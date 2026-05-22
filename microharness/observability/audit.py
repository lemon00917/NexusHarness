"""
Audit Log Module
================
Persistently log Guard approval/rejection operations to audit.log.

Usage:
    from audit import log_audit, get_audit_records
    log_audit(session_id, step, tool, args, approved=True)
    records = get_audit_records(limit=100)
"""

import json
from pathlib import Path
from datetime import datetime
from typing import Optional

# Audit log file location (project root)
_AUDIT_LOG_PATH: Optional[Path] = None

def _get_audit_path() -> Path:
    global _AUDIT_LOG_PATH
    if _AUDIT_LOG_PATH is None:
        _AUDIT_LOG_PATH = Path(__file__).parent.parent / "audit.log"
    return _AUDIT_LOG_PATH


def log_audit(
    session_id: str,
    step: int,
    tool: str,
    args: dict,
    approved: bool,
    operator: str = "human",
) -> None:
    """
    Write a single audit entry to audit.log.

    Args:
        session_id: Session identifier
        step: Step number in the harness loop
        tool: Tool name (e.g., "write_file", "weather")
        args: Tool arguments dict
        approved: True = approved, False = rejected
        operator: "human" or "auto"
    """
    entry = {
        "timestamp": datetime.now().isoformat(),
        "session_id": session_id,
        "step": step,
        "tool": tool,
        "args": args,
        "approved": approved,
        "operator": operator,
    }
    audit_path = _get_audit_path()
    with open(audit_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def get_audit_records(limit: int = 100) -> list[dict]:
    """
    Read recent audit records from audit.log.

    Args:
        limit: Maximum number of records to return (most recent)

    Returns:
        List of audit entry dicts, newest last
    """
    audit_path = _get_audit_path()
    if not audit_path.exists():
        return []

    records = []
    try:
        with open(audit_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    except Exception:
        return []

    return records[-limit:]


def clear_audit_log() -> None:
    """Clear all audit records (for testing)."""
    audit_path = _get_audit_path()
    if audit_path.exists():
        audit_path.unlink()