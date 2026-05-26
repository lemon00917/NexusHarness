"""
Replay Log Module
=================
Records step-by-step execution trace for replay and debugging.

Each session's execution is logged as JSON Lines in replays/{session_id}.jsonl
"""

import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, List

from typing_extensions import TypedDict, NotRequired, Literal

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────
# Types
# ──────────────────────────────────────────────────

class ReplayRecord(TypedDict):
    """A single step in the replay trace."""
    step: int
    timestamp: str
    type: Literal["agent", "tool_call", "tool_result", "approval", "interrupt", "complete"]
    llm_input: NotRequired[list]
    llm_output: NotRequired[dict]
    tool_name: NotRequired[str]
    tool_args: NotRequired[dict]
    tool_result: NotRequired[str]
    approval: NotRequired[Literal["auto_approve", "approved", "rejected"]]
    elapsed_ms: NotRequired[int]


# ──────────────────────────────────────────────────
# Paths
# ──────────────────────────────────────────────────

def _get_replays_dir() -> Path:
    """Return the replays directory path."""
    return Path(__file__).parent.parent / "replays"


# ──────────────────────────────────────────────────
# ReplayLogger
# ──────────────────────────────────────────────────

class ReplayLogger:
    """
    Thread-safe singleton replay logger.
    Records step-by-step execution trace for each session.
    """
    _instance: Optional["ReplayLogger"] = None
    _instance_lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._buffers: Dict[str, List[ReplayRecord]] = {}  # session_id -> pending records
        self._lock = threading.RLock()
        self._initialized = True
        self._replays_dir = _get_replays_dir()
        self._replays_dir.mkdir(parents=True, exist_ok=True)

    def start_session(self, session_id: str) -> None:
        """Start recording for a session."""
        with self._lock:
            if session_id not in self._buffers:
                self._buffers[session_id] = []
                logger.debug(f"Started replay session: {session_id}")

    def log_step(self, session_id: str, record: ReplayRecord) -> None:
        """Log a step record for a specific session."""
        if not session_id:
            raise ValueError("session_id is required")

        with self._lock:
            if session_id not in self._buffers:
                # Auto-start session if not started
                self._buffers[session_id] = []

            # Add timestamp if not present
            if "timestamp" not in record:
                record["timestamp"] = datetime.now().isoformat()

            self._buffers[session_id].append(record)

            # Auto-flush every 50 records
            if len(self._buffers[session_id]) >= 50:
                self._flush_session(session_id)

    def log_agent(self, session_id: str, step: int, llm_input: list,
                  llm_output: dict, elapsed_ms: int) -> None:
        """Log an agent thinking step."""
        self.log_step(session_id, {
            "step": step,
            "type": "agent",
            "llm_input": llm_input,
            "llm_output": llm_output,
            "elapsed_ms": elapsed_ms,
        })

    def log_tool_call(self, session_id: str, step: int, tool_name: str,
                      tool_args: dict, elapsed_ms: int) -> None:
        """Log a tool call step."""
        self.log_step(session_id, {
            "step": step,
            "type": "tool_call",
            "tool_name": tool_name,
            "tool_args": tool_args,
            "elapsed_ms": elapsed_ms,
        })

    def log_tool_result(self, session_id: str, step: int, tool_name: str,
                        result: str, elapsed_ms: int, max_length: int = 10000) -> None:
        """Log a tool result step."""
        # Truncate if needed, but log the truncation
        truncated = False
        if result and len(result) > max_length:
            result = result[:max_length] + f"\n... [truncated, original length: {len(result)}]"
            truncated = True
            logger.warning(f"Tool result truncated for {session_id}:{step}, tool={tool_name}")

        self.log_step(session_id, {
            "step": step,
            "type": "tool_result",
            "tool_name": tool_name,
            "tool_result": result if result else "",
            "elapsed_ms": elapsed_ms,
        })

    def log_approval(self, session_id: str, step: int, tool_name: str,
                     approval: Literal["auto_approve", "approved", "rejected"]) -> None:
        """Log an approval decision."""
        self.log_step(session_id, {
            "step": step,
            "type": "approval",
            "tool_name": tool_name,
            "approval": approval,
        })

    def log_complete(self, session_id: str, step: int) -> None:
        """Log session completion and flush."""
        self.log_step(session_id, {
            "step": step,
            "type": "complete",
        })
        self.flush(session_id)

    def log_interrupt(self, session_id: str, step: int) -> None:
        """Log session interrupt and flush."""
        self.log_step(session_id, {
            "step": step,
            "type": "interrupt",
        })
        self.flush(session_id)

    def _flush_session(self, session_id: str) -> None:
        """Flush buffer for a specific session to disk."""
        with self._lock:
            if session_id not in self._buffers or not self._buffers[session_id]:
                return

            records = self._buffers[session_id]
            self._buffers[session_id] = []  # Clear buffer before write to avoid double-write

        # Write to disk (outside lock to reduce contention)
        try:
            replay_file = self._replays_dir / f"{session_id}.jsonl"

            with open(replay_file, "a", encoding="utf-8") as f:
                for record in records:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")

            logger.debug(f"Flushed {len(records)} records for session {session_id}")

        except IOError as e:
            logger.error(f"Failed to flush session {session_id}: {e}")
            # Re-add records to buffer on failure
            with self._lock:
                self._buffers[session_id] = records + self._buffers[session_id]
            raise

    def flush(self, session_id: Optional[str] = None) -> None:
        """Flush buffer to disk for a specific session or all sessions."""
        if session_id:
            self._flush_session(session_id)
        else:
            with self._lock:
                session_ids = list(self._buffers.keys())
            for sid in session_ids:
                self._flush_session(sid)

    def get_replay(self, session_id: str) -> Optional[List[ReplayRecord]]:
        """
        Get full replay for a session. Loads from disk if available.
        """
        replay_file = self._replays_dir / f"{session_id}.jsonl"
        if not replay_file.exists():
            return None

        records = []
        try:
            with open(replay_file, "r", encoding="utf-8") as f:
                for line_num, line in enumerate(f, 1):
                    line = line.strip()
                    if line:
                        try:
                            records.append(json.loads(line))
                        except json.JSONDecodeError as e:
                            logger.error(f"Failed to parse line {line_num} in {session_id}.jsonl: {e}")
                            continue
            return records
        except IOError as e:
            logger.error(f"Failed to read replay for {session_id}: {e}")
            return None

    def get_replay_step(self, session_id: str, step: int) -> Optional[ReplayRecord]:
        """Get a specific step from replay."""
        records = self.get_replay(session_id)
        if not records:
            return None
        for r in records:
            if r.get("step") == step:
                return r
        return None

    def end_session(self, session_id: str) -> None:
        """End a session and cleanup in-memory buffer."""
        self.flush(session_id)
        with self._lock:
            if session_id in self._buffers:
                del self._buffers[session_id]
                logger.debug(f"Ended replay session: {session_id}")

    def clear_session(self, session_id: str) -> None:
        """Clear in-memory buffer for a session (does not delete disk file)."""
        with self._lock:
            if session_id in self._buffers:
                self._buffers[session_id] = []
                logger.debug(f"Cleared buffer for session {session_id}")


# ──────────────────────────────────────────────────
# Global accessor
# ──────────────────────────────────────────────────

def get_replay_logger() -> ReplayLogger:
    """Get the global ReplayLogger instance."""
    return ReplayLogger()


def load_replay_from_disk(session_id: str) -> Optional[List[ReplayRecord]]:
    """Load replay records from disk for a session."""
    return get_replay_logger().get_replay(session_id)