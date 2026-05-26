"""
Audit Log Module
================
Persistent, thread-safe audit logging for Guard approval/rejection operations.

Features:
- Thread-safe log writing
- Atomic writes (no partial/corrupted entries)
- Automatic log rotation
- Structured querying and filtering
- Backup and restore capabilities

Usage:
    from audit import AuditLogger

    logger = AuditLogger()
    logger.log(session_id="abc", step=1, tool="write_file",
               args={"path": "/tmp/test"}, approved=True)

    records = logger.query(limit=100, session_id="abc")
"""

import json
import os
import threading
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Callable


# ──────────────────────── Configuration ────────────────────────

# Default audit log file
DEFAULT_AUDIT_LOG = "audit.log"

# Maximum log file size before rotation (10 MB)
MAX_LOG_SIZE_BYTES = 10 * 1024 * 1024

# Number of rotated backups to keep
MAX_BACKUP_COUNT = 5

# Valid operator types
OPERATOR_HUMAN = "human"
OPERATOR_AUTO = "auto"
VALID_OPERATORS = {OPERATOR_HUMAN, OPERATOR_AUTO}


# ──────────────────────── Data Model ────────────────────────

@dataclass
class AuditEntry:
    """
    Single audit log entry.

    Attributes:
        timestamp: ISO 8601 timestamp of the operation
        session_id: Session identifier
        step: Step number in the harness loop
        tool: Tool name (e.g., "write_file", "weather")
        args: Tool arguments dictionary
        approved: True = approved, False = rejected
        operator: "human" or "auto"
    """
    timestamp: str
    session_id: str
    step: int
    tool: str
    args: dict
    approved: bool
    operator: str

    @classmethod
    def create(
        cls,
        session_id: str,
        step: int,
        tool: str,
        args: dict,
        approved: bool,
        operator: str = OPERATOR_HUMAN,
        timestamp: Optional[str] = None,
    ) -> "AuditEntry":
        """
        Create a validated audit entry.

        Args:
            session_id: Session identifier
            step: Step number
            tool: Tool name
            args: Tool arguments
            approved: Whether operation was approved
            operator: Who made the decision
            timestamp: ISO timestamp (auto-generated if None)

        Returns:
            Validated AuditEntry

        Raises:
            ValueError: If arguments are invalid
        """
        # Validate required fields
        if not session_id or not isinstance(session_id, str):
            raise ValueError(f"session_id must be a non-empty string, got: {session_id}")

        if not tool or not isinstance(tool, str):
            raise ValueError(f"tool must be a non-empty string, got: {tool}")

        if not isinstance(args, dict):
            raise ValueError(f"args must be a dictionary, got: {type(args)}")

        if operator not in VALID_OPERATORS:
            raise ValueError(
                f"operator must be one of {VALID_OPERATORS}, got: {operator}"
            )

        if timestamp is None:
            timestamp = datetime.now().isoformat()

        return cls(
            timestamp=timestamp,
            session_id=session_id,
            step=step,
            tool=tool,
            args=args,
            approved=approved,
            operator=operator,
        )

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return asdict(self)

    def to_json(self) -> str:
        """Serialize to JSON string."""
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: dict) -> "AuditEntry":
        """Create from dictionary."""
        return cls(**data)


# ──────────────────────── Audit Logger ────────────────────────

class AuditLogger:
    """
    Thread-safe audit logger with automatic rotation.

    Features:
    - Thread-safe writes using locks
    - Atomic writes (complete line per write)
    - Automatic log rotation based on file size
    - Backup and restore capabilities

    Usage:
        logger = AuditLogger("custom_audit.log")
        logger.log(session_id="abc", step=1, tool="write_file",
                   args={}, approved=True)
    """

    def __init__(self, log_path: Optional[str] = None):
        """
        Initialize the audit logger.

        Args:
            log_path: Path to audit log file.
                     Defaults to 'audit.log' in project root.
        """
        if log_path is None:
            log_path = str(Path(__file__).parent.parent / DEFAULT_AUDIT_LOG)

        self._log_path = Path(log_path)
        self._write_lock = threading.Lock()
        self._max_size = MAX_LOG_SIZE_BYTES
        self._max_backups = MAX_BACKUP_COUNT

    # ──────────────────────── Public API ────────────────────────

    @property
    def path(self) -> Path:
        """Get the log file path."""
        return self._log_path

    @property
    def size_bytes(self) -> int:
        """Get current log file size in bytes."""
        try:
            return self._log_path.stat().st_size
        except FileNotFoundError:
            return 0

    def log(
        self,
        session_id: str,
        step: int,
        tool: str,
        args: dict,
        approved: bool,
        operator: str = OPERATOR_HUMAN,
    ) -> AuditEntry:
        """
        Write a single audit entry to the log.

        Args:
            session_id: Session identifier
            step: Step number in the harness loop
            tool: Tool name (e.g., "write_file")
            args: Tool arguments dictionary
            approved: True = approved, False = rejected
            operator: "human" or "auto"

        Returns:
            The created AuditEntry

        Raises:
            ValueError: If arguments are invalid
            OSError: If file cannot be written
        """
        # Create and validate entry
        entry = AuditEntry.create(
            session_id=session_id,
            step=step,
            tool=tool,
            args=args,
            approved=approved,
            operator=operator,
        )

        # Write atomically with thread safety
        with self._write_lock:
            self._rotate_if_needed()
            self._write_entry(entry)

        return entry

    def query(
        self,
        limit: int = 100,
        session_id: Optional[str] = None,
        tool: Optional[str] = None,
        approved: Optional[bool] = None,
        operator: Optional[str] = None,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
    ) -> List[AuditEntry]:
        """
        Query audit records with optional filters.

        Args:
            limit: Maximum records to return (newest last)
            session_id: Filter by session
            tool: Filter by tool name
            approved: Filter by approval status
            operator: Filter by operator type
            since: Filter records after this time
            until: Filter records before this time

        Returns:
            List of matching AuditEntry objects

        Examples:
            # Get last 50 records for a specific session
            logger.query(limit=50, session_id="session_123")

            # Get all rejected operations today
            logger.query(
                approved=False,
                since=datetime.now().replace(hour=0, minute=0)
            )
        """
        records = self._read_all_entries()

        # Apply filters
        if session_id:
            records = [r for r in records if r.session_id == session_id]

        if tool:
            records = [r for r in records if r.tool == tool]

        if approved is not None:
            records = [r for r in records if r.approved == approved]

        if operator:
            records = [r for r in records if r.operator == operator]

        if since:
            records = [r for r in records if self._parse_timestamp(r.timestamp) >= since]

        if until:
            records = [r for r in records if self._parse_timestamp(r.timestamp) <= until]

        # Return most recent records
        return records[-limit:]

    def get_statistics(self) -> dict:
        """
        Get summary statistics of the audit log.

        Returns:
            Dictionary with statistics:
            - total_entries: Total number of log entries
            - approved_count: Number of approved operations
            - rejected_count: Number of rejected operations
            - unique_sessions: Number of unique sessions
            - unique_tools: Set of tool names used
            - first_entry: Timestamp of earliest entry
            - last_entry: Timestamp of latest entry
        """
        records = self._read_all_entries()

        if not records:
            return {
                "total_entries": 0,
                "message": "Audit log is empty"
            }

        approved = sum(1 for r in records if r.approved)
        rejected = sum(1 for r in records if not r.approved)

        return {
            "total_entries": len(records),
            "approved_count": approved,
            "rejected_count": rejected,
            "approval_rate": f"{(approved / len(records) * 100):.1f}%" if records else "N/A",
            "unique_sessions": len(set(r.session_id for r in records)),
            "unique_tools": sorted(set(r.tool for r in records)),
            "operators": {
                OPERATOR_HUMAN: sum(1 for r in records if r.operator == OPERATOR_HUMAN),
                OPERATOR_AUTO: sum(1 for r in records if r.operator == OPERATOR_AUTO),
            },
            "first_entry": records[0].timestamp,
            "last_entry": records[-1].timestamp,
        }

    def clear(self) -> None:
        """
        Clear all audit records.

        Creates a backup before clearing.

        Raises:
            OSError: If backup or clear operation fails
        """
        if not self._log_path.exists():
            return

        # Create timestamped backup before clearing
        self._backup()

        with self._write_lock:
            self._log_path.unlink()

    def backup(self, backup_path: Optional[str] = None) -> Path:
        """
        Create a backup of the audit log.

        Args:
            backup_path: Custom backup path (auto-generated if None)

        Returns:
            Path to the backup file
        """
        with self._write_lock:
            return self._backup(backup_path)

    def restore(self, backup_path: str) -> int:
        """
        Restore audit log from a backup file.

        Args:
            backup_path: Path to backup file to restore

        Returns:
            Number of entries restored

        Raises:
            FileNotFoundError: If backup file doesn't exist
            ValueError: If backup file is invalid
        """
        backup_path = Path(backup_path)

        if not backup_path.exists():
            raise FileNotFoundError(f"Backup file not found: {backup_path}")

        # Validate backup file
        entries = self._read_entries_from_file(backup_path)
        if not entries:
            raise ValueError(f"Backup file contains no valid entries: {backup_path}")

        with self._write_lock:
            # Backup current log before restoring
            if self._log_path.exists():
                self._backup()

            # Copy backup to current log
            import shutil
            shutil.copy2(backup_path, self._log_path)

        return len(entries)

    def tail(self, n: int = 10) -> List[AuditEntry]:
        """
        Get the most recent n entries.

        Args:
            n: Number of entries to return

        Returns:
            List of the n most recent AuditEntry objects
        """
        return self.query(limit=n)

    def search_args(self, key: str, value: str, limit: int = 100) -> List[AuditEntry]:
        """
        Search for entries where args contain a specific key-value pair.

        Args:
            key: Argument key to search for
            value: Expected value (string match)
            limit: Maximum results

        Returns:
            Matching AuditEntry objects

        Example:
            # Find all operations on a specific file
            logger.search_args("path", "/tmp/test.txt")
        """
        records = self._read_all_entries()

        matches = []
        for record in records:
            arg_value = record.args.get(key)
            if arg_value is not None and str(arg_value) == value:
                matches.append(record)

        return matches[-limit:]

    # ──────────────────────── Private Methods ────────────────────────

    def _write_entry(self, entry: AuditEntry) -> None:
        """
        Write a single entry to the log file.

        Args:
            entry: AuditEntry to write

        Raises:
            OSError: If write fails
        """
        try:
            with open(self._log_path, "a", encoding="utf-8") as f:
                f.write(entry.to_json() + "\n")
                f.flush()  # Ensure write to disk
                os.fsync(f.fileno())  # Force OS-level flush
        except OSError as e:
            raise OSError(f"Failed to write audit entry: {e}")

    def _read_all_entries(self) -> List[AuditEntry]:
        """
        Read all valid entries from the log file.

        Returns:
            List of AuditEntry objects (chronological order)
        """
        return self._read_entries_from_file(self._log_path)

    @staticmethod
    def _read_entries_from_file(file_path: Path) -> List[AuditEntry]:
        """
        Read entries from a specific file.

        Args:
            file_path: Path to read from

        Returns:
            List of AuditEntry objects
        """
        if not file_path.exists():
            return []

        entries = []

        try:
            with open(file_path, encoding="utf-8") as f:
                for line_num, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue

                    try:
                        data = json.loads(line)
                        entry = AuditEntry.from_dict(data)
                        entries.append(entry)
                    except json.JSONDecodeError:
                        # Skip corrupted lines
                        continue
                    except (TypeError, KeyError) as e:
                        # Skip malformed entries
                        continue

        except Exception:
            return []

        return entries

    def _rotate_if_needed(self) -> None:
        """
        Check if log rotation is needed and perform it.

        Rotation is triggered when the log file exceeds max_size.
        Note: Must be called within _write_lock to ensure thread safety.
        """
        if not self._log_path.exists():
            return

        if self._log_path.stat().st_size < self._max_size:
            return

        # Rotate existing backups (audit.log.1 -> audit.log.2, etc.)
        for i in range(self._max_backups - 1, 0, -1):
            old = self._log_path.with_suffix(f".log.{i}")
            new = self._log_path.with_suffix(f".log.{i + 1}")
            if old.exists():
                old.rename(new)

        # Rotate current log to .1
        backup = self._log_path.with_suffix(".log.1")
        self._log_path.rename(backup)

        # New log will be created on next write

    def _backup(self, backup_path: Optional[str] = None) -> Path:
        """
        Create a backup of the current log file.

        Args:
            backup_path: Custom backup path

        Returns:
            Path to the backup file
        """
        if not self._log_path.exists():
            if backup_path:
                Path(backup_path).touch()
            return Path(backup_path) if backup_path else self._log_path

        if backup_path is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = str(
                self._log_path.parent / f"audit_backup_{timestamp}.log"
            )

        import shutil
        shutil.copy2(self._log_path, backup_path)

        return Path(backup_path)

    @staticmethod
    def _parse_timestamp(timestamp_str: str) -> datetime:
        """
        Parse an ISO timestamp string to datetime.

        Args:
            timestamp_str: ISO 8601 timestamp

        Returns:
            datetime object
        """
        try:
            return datetime.fromisoformat(timestamp_str)
        except (ValueError, TypeError):
            return datetime.min


# ──────────────────────── Module-Level API ────────────────────────

# Default logger instance for backward compatibility
_default_logger: Optional[AuditLogger] = None


def _get_default_logger() -> AuditLogger:
    """Get or create the default audit logger."""
    global _default_logger
    if _default_logger is None:
        _default_logger = AuditLogger()
    return _default_logger


def log_audit(
    session_id: str,
    step: int,
    tool: str,
    args: dict,
    approved: bool,
    operator: str = "human",
) -> None:
    """
    Write a single audit entry (module-level convenience function).

    Args:
        session_id: Session identifier
        step: Step number in the harness loop
        tool: Tool name
        args: Tool arguments
        approved: True = approved, False = rejected
        operator: "human" or "auto"
    """
    _get_default_logger().log(
        session_id=session_id,
        step=step,
        tool=tool,
        args=args,
        approved=approved,
        operator=operator,
    )


def get_audit_records(limit: int = 100) -> List[dict]:
    """
    Read recent audit records (module-level convenience function).

    Args:
        limit: Maximum number of records to return

    Returns:
        List of audit entry dicts
    """
    entries = _get_default_logger().query(limit=limit)
    return [entry.to_dict() for entry in entries]


def clear_audit_log() -> None:
    """Clear all audit records (module-level convenience function)."""
    _get_default_logger().clear()


# ──────────────────────── Advanced Query Helpers ────────────────────────

def get_approval_rate(session_id: Optional[str] = None) -> float:
    """
    Calculate the approval rate as a percentage.

    Args:
        session_id: Optional session to filter by (reserved for future use)

    Returns:
        Approval rate percentage (0.0-100.0), or 0.0 if no records
    """
    # Note: session_id filtering could be done via query() but for
    # simplicity we use aggregate stats. Future: add session filter.
    stats = _get_default_logger().get_statistics()

    total = stats.get("total_entries", 0)
    if total == 0:
        return 0.0

    return (stats["approved_count"] / total) * 100


def get_recent_rejections(limit: int = 10) -> List[AuditEntry]:
    """
    Get the most recent rejected operations.

    Args:
        limit: Maximum number of results

    Returns:
        List of rejected AuditEntry objects
    """
    return _get_default_logger().query(
        approved=False,
        limit=limit,
    )