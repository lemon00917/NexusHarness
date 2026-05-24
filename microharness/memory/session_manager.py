"""
Session Manager Module
======================
Manages multiple concurrent agent sessions with interrupt/resume capability.

Features:
- Create/delete/list sessions
- HarnessState persistence per session
- Interrupt signal support (check at each step)
- Disk persistence to conversations/ directory

Usage:
    from session_manager import get_session_manager, SessionState
    sm = get_session_manager()
    session = sm.create_session("Write a Fibonacci script")
    sm.set_interrupted(session["session_id"])
    state = sm.get_session(session["session_id"])
"""

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from typing_extensions import TypedDict, NotRequired, Literal

# ──────────────────────────────────────────────────
# Types
# ──────────────────────────────────────────────────

class HarnessState(TypedDict):
    """Represents the current state of a harness execution."""
    messages: list
    step_count: int
    approved: bool


class SessionState(TypedDict):
    """Full session state persisted to disk."""
    session_id: str
    task: str
    status: Literal["active", "completed", "interrupted", "paused"]
    created_at: str
    updated_at: str
    harness_state: HarnessState
    interrupted: bool


# ──────────────────────────────────────────────────
# Paths
# ──────────────────────────────────────────────────

def _get_conversations_dir() -> Path:
    """Return the conversations directory path."""
    return Path(__file__).parent.parent / "conversations"


# ──────────────────────────────────────────────────
# SessionManager
# ──────────────────────────────────────────────────

class SessionManager:
    """
    Singleton session manager — manages all active sessions.

    In-memory cache + disk persistence to conversations/*.json
    """
    _instance: Optional["SessionManager"] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._sessions: dict[str, SessionState] = {}
        self._initialized = True

    # ── Session Lifecycle ──────────────────────────

    def create_session(self, task: str) -> SessionState:
        """
        Create a new session with initial state.

        Args:
            task: The user's task for this session

        Returns:
            The newly created SessionState
        """
        session_id = f"session_{int(time.time() * 1000)}"
        now = datetime.now().isoformat()

        state: SessionState = {
            "session_id": session_id,
            "task": task,
            "status": "active",
            "created_at": now,
            "updated_at": now,
            "harness_state": {
                "messages": [],
                "step_count": 0,
                "approved": True,
            },
            "interrupted": False,
        }

        self._sessions[session_id] = state
        save_session_to_disk(state)
        return state

    def get_session(self, session_id: str) -> Optional[SessionState]:
        """
        Get a session by ID. Loads from disk if not in memory.

        Args:
            session_id: The session ID

        Returns:
            SessionState or None if not found
        """
        if session_id in self._sessions:
            return self._sessions[session_id]

        # Try to load from disk
        state = self.load_from_disk(session_id)
        if state:
            self._sessions[session_id] = state
        return state

    def update_session(self, session_id: str, harness_state: HarnessState) -> bool:
        """
        Update a session's harness state and save to disk.

        Args:
            session_id: The session ID
            harness_state: The new HarnessState to save

        Returns:
            True if updated, False if session not found
        """
        session = self.get_session(session_id)
        if not session:
            return False

        session["harness_state"] = harness_state
        session["updated_at"] = datetime.now().isoformat()
        save_session_to_disk(session)
        return True

    def set_status(self, session_id: str, status: Literal["active", "completed", "interrupted", "paused"]) -> bool:
        """
        Update a session's status.

        Args:
            session_id: The session ID
            status: New status

        Returns:
            True if updated, False if session not found
        """
        session = self.get_session(session_id)
        if not session:
            return False

        session["status"] = status
        session["updated_at"] = datetime.now().isoformat()
        save_session_to_disk(session)
        return True

    def delete_session(self, session_id: str) -> bool:
        """
        Delete a session (from memory and disk).

        Args:
            session_id: The session ID

        Returns:
            True if deleted, False if not found
        """
        if session_id in self._sessions:
            del self._sessions[session_id]

        conv_dir = _get_conversations_dir()
        conv_file = conv_dir / f"{session_id}.json"
        if conv_file.exists():
            conv_file.unlink()
            return True
        return False

    def list_sessions(self) -> list[SessionState]:
        """
        List all sessions sorted by updated_at (newest first).
        Loads from disk for any sessions not in memory.

        Returns:
            List of SessionState dicts
        """
        conv_dir = _get_conversations_dir()
        conv_dir.mkdir(exist_ok=True)

        # Collect all session files
        sessions: list[SessionState] = []
        for f in sorted(conv_dir.glob("session_*.json"), key=lambda x: -x.stat().st_mtime):
            try:
                session_id = f.stem
                session = self.get_session(session_id)
                if session:
                    sessions.append(session)
            except Exception:
                continue

        return sessions

    # ── Interrupt Support ──────────────────────────

    def set_interrupted(self, session_id: str) -> bool:
        """
        Set the interrupt flag for a session.

        Args:
            session_id: The session ID

        Returns:
            True if set, False if session not found
        """
        session = self.get_session(session_id)
        if not session:
            return False

        session["interrupted"] = True
        session["status"] = "interrupted"
        session["updated_at"] = datetime.now().isoformat()
        save_session_to_disk(session)
        return True

    def clear_interrupted(self, session_id: str) -> bool:
        """
        Clear the interrupt flag for a session.

        Args:
            session_id: The session ID

        Returns:
            True if cleared, False if session not found
        """
        session = self.get_session(session_id)
        if not session:
            return False

        session["interrupted"] = False
        session["updated_at"] = datetime.now().isoformat()
        save_session_to_disk(session)
        return True

    def is_interrupted(self, session_id: str) -> bool:
        """
        Check if a session has been interrupted.

        Args:
            session_id: The session ID

        Returns:
            True if interrupted, False otherwise
        """
        session = self.get_session(session_id)
        if not session:
            return False
        return session.get("interrupted", False)

    # ──────────────────────────────────────────────────
# Helpers for JSON serialization
# ──────────────────────────────────────────────────

    def load_from_disk(self, session_id: str) -> Optional[SessionState]:
        """Load a session state from disk."""
        conv_dir = _get_conversations_dir()
        conv_file = conv_dir / f'{session_id}.json'

        if not conv_file.exists():
            return None

        try:
            with open(conv_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return None


def _serialize_messages(msgs: list) -> list:
    """Convert LangChain message objects to JSON-serializable dicts."""
    result = []
    for m in msgs:
        if isinstance(m, dict):
            result.append(m)
        elif hasattr(m, 'content') and hasattr(m, 'type'):
            msg_dict = {"type": getattr(m, 'type', 'unknown'), "content": str(m.content) if m.content else ''}
            if hasattr(m, 'tool_calls') and m.tool_calls:
                msg_dict["tool_calls"] = m.tool_calls
            if hasattr(m, 'tool_call_id'):
                msg_dict["tool_call_id"] = m.tool_call_id
            result.append(msg_dict)
        else:
            result.append({"type": "unknown", "content": str(m)})
    return result


def _serialize_state(state: dict) -> dict:
    """Serialize HarnessState for JSON storage."""
    return {
        "messages": _serialize_messages(state.get("messages", [])),
        "step_count": state.get("step_count", 0),
        "approved": state.get("approved", True),
    }


# ──────────────────────────────────────────────────
# Disk Persistence
# ──────────────────────────────────────────────────

def save_session_to_disk(state: SessionState) -> None:
    """
    Save a session state to disk.

    Args:
        state: The SessionState to save
    """
    conv_dir = _get_conversations_dir()
    conv_dir.mkdir(exist_ok=True)

    # Serialize harness_state to ensure JSON compatibility
    serializable = dict(state)
    serializable["harness_state"] = _serialize_state(state["harness_state"])

    conv_file = conv_dir / f"{state['session_id']}.json"
    with open(conv_file, "w", encoding="utf-8") as f:
        json.dump(serializable, f, ensure_ascii=False, indent=2)


# ──────────────────────────────────────────────────
# Global accessor
# ──────────────────────────────────────────────────

_session_manager: Optional[SessionManager] = None


def get_session_manager() -> SessionManager:
    """Get the global SessionManager instance."""
    global _session_manager
    if _session_manager is None:
        _session_manager = SessionManager()
    return _session_manager
