"""
Replay Log Module
=================
Records step-by-step execution trace for replay and debugging.

Each session's execution is logged as JSON Lines in replays/{session_id}.jsonl
Each line is a ReplayRecord with:
  - step: step number
  - timestamp: ISO format
  - type: agent | tool_call | tool_result | approval | interrupt | complete
  - llm_input: messages sent to LLM
  - llm_output: LLM response (content + tool_calls)
  - tool_name: name of tool called
  - tool_args: tool arguments
  - tool_result: tool execution result
  - approval: auto_approve | approved | rejected
  - elapsed_ms: time for this step

Usage:
    from replay_log import get_replay_logger
    logger = get_replay_logger()
    logger.start_session("session_123")
    logger.log_step({"step": 1, "type": "agent", ...})
    logger.flush()
"""

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Literal

from typing_extensions import TypedDict, NotRequired

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
    approval: NotRequired[str]
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
    Singleton replay logger.
    Records step-by-step execution trace for each session.
    Flushes to disk periodically or at session end.
    """
    _instance: Optional["ReplayLogger"] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._sessions: dict[str, list[ReplayRecord]] = {}  # session_id -> records
        self._buffers: dict[str, list[ReplayRecord]] = {}     # session_id -> pending buffer
        self._initialized = True

    def start_session(self, session_id: str) -> None:
        """Start recording for a session."""
        if session_id not in self._sessions:
            self._sessions[session_id] = []
        if session_id not in self._buffers:
            self._buffers[session_id] = []
        self._buffers[session_id] = []  # Clear buffer for fresh start

    def log_step(self, record: ReplayRecord) -> None:
        """
        Log a step record. Records are buffered in memory.
        Call flush() to write to disk.
        """
        session_id = None
        # Find session from record if present
        if "session_id" in record:
            session_id = record.pop("session_id")

        # If no session_id, use the last active session (not ideal but works)
        if not session_id and self._buffers:
            session_id = list(self._buffers.keys())[-1] if self._buffers else None

        if not session_id:
            return  # Can't log without session

        if session_id not in self._buffers:
            self._buffers[session_id] = []

        # Add timestamp if not present
        if "timestamp" not in record:
            record["timestamp"] = datetime.now().isoformat()

        self._buffers[session_id].append(record)

        # Auto-flush every 10 records
        if len(self._buffers[session_id]) >= 10:
            self._flush_session(session_id)

    def log_agent(self, session_id: str, step: int, llm_input: list, llm_output: dict, elapsed_ms: int) -> None:
        """Log an agent thinking step."""
        record: ReplayRecord = {
            "step": step,
            "timestamp": datetime.now().isoformat(),
            "type": "agent",
            "llm_input": llm_input,
            "llm_output": llm_output,
            "elapsed_ms": elapsed_ms,
        }
        if session_id not in self._buffers:
            self._buffers[session_id] = []
        self._buffers[session_id].append(record)

    def log_tool_call(self, session_id: str, step: int, tool_name: str, tool_args: dict, elapsed_ms: int) -> None:
        """Log a tool call step."""
        record: ReplayRecord = {
            "step": step,
            "timestamp": datetime.now().isoformat(),
            "type": "tool_call",
            "tool_name": tool_name,
            "tool_args": tool_args,
            "elapsed_ms": elapsed_ms,
        }
        if session_id not in self._buffers:
            self._buffers[session_id] = []
        self._buffers[session_id].append(record)

    def log_tool_result(self, session_id: str, step: int, tool_name: str, result: str, elapsed_ms: int) -> None:
        """Log a tool result step."""
        record: ReplayRecord = {
            "step": step,
            "timestamp": datetime.now().isoformat(),
            "type": "tool_result",
            "tool_name": tool_name,
            "tool_result": result[:2000] if result else "",  # Truncate long output
            "elapsed_ms": elapsed_ms,
        }
        if session_id not in self._buffers:
            self._buffers[session_id] = []
        self._buffers[session_id].append(record)

    def log_approval(self, session_id: str, step: int, tool_name: str, approval: str) -> None:
        """Log an approval decision."""
        record: ReplayRecord = {
            "step": step,
            "timestamp": datetime.now().isoformat(),
            "type": "approval",
            "tool_name": tool_name,
            "approval": approval,
        }
        if session_id not in self._buffers:
            self._buffers[session_id] = []
        self._buffers[session_id].append(record)

    def log_complete(self, session_id: str, step: int, reason: str) -> None:
        """Log session completion."""
        record: ReplayRecord = {
            "step": step,
            "timestamp": datetime.now().isoformat(),
            "type": "complete",
        }
        if session_id not in self._buffers:
            self._buffers[session_id] = []
        self._buffers[session_id].append(record)
        self._flush_session(session_id)

    def log_interrupt(self, session_id: str, step: int) -> None:
        """Log session interrupt."""
        record: ReplayRecord = {
            "step": step,
            "timestamp": datetime.now().isoformat(),
            "type": "interrupt",
        }
        if session_id not in self._buffers:
            self._buffers[session_id] = []
        self._buffers[session_id].append(record)
        self._flush_session(session_id)

    def _flush_session(self, session_id: str) -> None:
        """Flush buffer for a specific session to disk."""
        if session_id not in self._buffers or not self._buffers[session_id]:
            return

        records = self._buffers[session_id]
        self._buffers[session_id] = []

        # Append to in-memory records
        if session_id not in self._sessions:
            self._sessions[session_id] = []
        self._sessions[session_id].extend(records)

        # Write to disk as JSON Lines
        replays_dir = _get_replays_dir()
        replays_dir.mkdir(exist_ok=True)
        replay_file = replays_dir / f"{session_id}.jsonl"

        with open(replay_file, "a", encoding="utf-8") as f:
            for record in records:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def flush(self, session_id: Optional[str] = None) -> None:
        """Flush buffer to disk for a specific session or all sessions."""
        if session_id:
            self._flush_session(session_id)
        else:
            for sid in list(self._buffers.keys()):
                self._flush_session(sid)

    def get_replay(self, session_id: str) -> Optional[list[ReplayRecord]]:
        """
        Get full replay for a session. Loads from memory cache first,
        then from disk if not fully cached.
        """
        records = self._sessions.get(session_id, [])

        # If we have fewer records than expected, try loading from disk
        replay_file = _get_replays_dir() / f"{session_id}.jsonl"
        if replay_file.exists() and len(records) == 0:
            # Load all from disk
            loaded = []
            try:
                with open(replay_file, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            loaded.append(json.loads(line))
            except (json.JSONDecodeError, IOError):
                return records if records else None
            self._sessions[session_id] = loaded
            return loaded

        return records if records else None

    def get_replay_step(self, session_id: str, step: int) -> Optional[ReplayRecord]:
        """Get a specific step from replay."""
        records = self.get_replay(session_id)
        if not records:
            return None
        for r in records:
            if r.get("step") == step:
                return r
        return None

    def clear_session(self, session_id: str) -> None:
        """Clear in-memory records for a session (does not delete disk file)."""
        if session_id in self._sessions:
            del self._sessions[session_id]
        if session_id in self._buffers:
            del self._buffers[session_id]


# ──────────────────────────────────────────────────
# Global accessor
# ──────────────────────────────────────────────────

_replay_logger: Optional[ReplayLogger] = None


def get_replay_logger() -> ReplayLogger:
    """Get the global ReplayLogger instance."""
    global _replay_logger
    if _replay_logger is None:
        _replay_logger = ReplayLogger()
    return _replay_logger


def load_replay_from_disk(session_id: str) -> Optional[list[ReplayRecord]]:
    """Load replay records from disk for a session."""
    replay_file = _get_replays_dir() / f"{session_id}.jsonl"
    if not replay_file.exists():
        return None

    records = []
    try:
        with open(replay_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
        return records
    except (json.JSONDecodeError, IOError):
        return None