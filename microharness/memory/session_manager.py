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
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from typing_extensions import TypedDict, NotRequired, Literal

# Setup logging
logger = logging.getLogger(__name__)

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
    Thread-safe singleton session manager — manages all active sessions.
    In-memory cache + disk persistence to conversations/*.json
    """
    _instance: Optional["SessionManager"] = None
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
        self._sessions: dict[str, SessionState] = {}
        self._lock = threading.RLock()
        self._initialized = True
        # Ensure conversations directory exists
        _get_conversations_dir().mkdir(parents=True, exist_ok=True)

    # ── Session Lifecycle ──────────────────────────

    def create_session(self, task: str) -> SessionState:
        """Create a new session with initial state."""
        session_id = f"session_{int(time.time() * 1000)}_{threading.get_ident()}"
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

        with self._lock:
            self._sessions[session_id] = state
        self._save_session_to_disk(state)
        return state

    def get_session(self, session_id: str) -> Optional[SessionState]:
        """Get a session by ID. Loads from disk if not in memory."""
        with self._lock:
            if session_id in self._sessions:
                return self._sessions[session_id].copy()

        # Try to load from disk
        state = self._load_from_disk(session_id)
        if state:
            with self._lock:
                self._sessions[session_id] = state
            return state.copy()
        return None

    def update_session(self, session_id: str, harness_state: HarnessState) -> bool:
        """Update a session's harness state and save to disk."""
        with self._lock:
            if session_id not in self._sessions:
                state = self._load_from_disk(session_id)
                if not state:
                    return False
                self._sessions[session_id] = state

            self._sessions[session_id]["harness_state"] = harness_state
            self._sessions[session_id]["updated_at"] = datetime.now().isoformat()
            session_to_save = self._sessions[session_id].copy()

        self._save_session_to_disk(session_to_save)
        return True

    def set_status(self, session_id: str, status: Literal["active", "completed", "interrupted", "paused"]) -> bool:
        """Update a session's status."""
        with self._lock:
            if session_id not in self._sessions:
                state = self._load_from_disk(session_id)
                if not state:
                    return False
                self._sessions[session_id] = state

            self._sessions[session_id]["status"] = status
            self._sessions[session_id]["updated_at"] = datetime.now().isoformat()
            session_to_save = self._sessions[session_id].copy()

        self._save_session_to_disk(session_to_save)
        return True

    def delete_session(self, session_id: str) -> bool:
        """Delete a session (from memory and disk)."""
        with self._lock:
            if session_id in self._sessions:
                del self._sessions[session_id]

        conv_file = _get_conversations_dir() / f"{session_id}.json"
        if conv_file.exists():
            try:
                conv_file.unlink()
            except OSError as e:
                logger.error(f"Failed to delete {session_id}.json: {e}")
                return False
            return True
        return False

    def list_sessions(self) -> list[SessionState]:
        """List all sessions sorted by updated_at (newest first)."""
        conv_dir = _get_conversations_dir()
        sessions: list[SessionState] = []

        for f in sorted(conv_dir.glob("session_*.json"), key=lambda x: -x.stat().st_mtime):
            try:
                session = self.get_session(f.stem)
                if session:
                    sessions.append(session)
            except Exception as e:
                logger.warning(f"Failed to load {f.name}: {e}")
                continue

        return sessions

    # ── Interrupt Support ──────────────────────────

    def set_interrupted(self, session_id: str) -> bool:
        """Set the interrupt flag for a session."""
        with self._lock:
            if session_id not in self._sessions:
                state = self._load_from_disk(session_id)
                if not state:
                    return False
                self._sessions[session_id] = state

            self._sessions[session_id]["interrupted"] = True
            self._sessions[session_id]["status"] = "interrupted"
            self._sessions[session_id]["updated_at"] = datetime.now().isoformat()
            session_to_save = self._sessions[session_id].copy()

        self._save_session_to_disk(session_to_save)
        return True

    def clear_interrupted(self, session_id: str, restore_status: str = "paused") -> bool:
        """Clear the interrupt flag for a session."""
        with self._lock:
            if session_id not in self._sessions:
                state = self._load_from_disk(session_id)
                if not state:
                    return False
                self._sessions[session_id] = state

            self._sessions[session_id]["interrupted"] = False
            self._sessions[session_id]["status"] = restore_status
            self._sessions[session_id]["updated_at"] = datetime.now().isoformat()
            session_to_save = self._sessions[session_id].copy()

        self._save_session_to_disk(session_to_save)
        return True

    def is_interrupted(self, session_id: str) -> bool:
        """Check if a session has been interrupted."""
        with self._lock:
            if session_id in self._sessions:
                return self._sessions[session_id].get("interrupted", False)
            state = self._load_from_disk(session_id)
            return state.get("interrupted", False) if state else False

    # ── Private Methods ────────────────────────────

    def _load_from_disk(self, session_id: str) -> Optional[SessionState]:
        """Load a session state from disk."""
        conv_file = _get_conversations_dir() / f'{session_id}.json'
        if not conv_file.exists():
            return None

        try:
            with open(conv_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.error(f"Failed to load {session_id}: {e}")
            return None

    def _save_session_to_disk(self, state: SessionState) -> None:
        """Save a session state to disk."""
        conv_dir = _get_conversations_dir()
        conv_dir.mkdir(exist_ok=True)

        # Serialize harness_state to ensure JSON compatibility
        serializable = dict(state)
        serializable["harness_state"] = self._serialize_state(state["harness_state"])

        conv_file = conv_dir / f"{state['session_id']}.json"

        # Atomic write via temp file
        temp_file = conv_file.with_suffix('.tmp')
        try:
            with open(temp_file, 'w', encoding='utf-8') as f:
                json.dump(serializable, f, ensure_ascii=False, indent=2)
            temp_file.replace(conv_file)
        except IOError as e:
            logger.error(f"Failed to save {state['session_id']}: {e}")
            if temp_file.exists():
                temp_file.unlink()
            raise

    @staticmethod
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

    def _serialize_state(self, state: dict) -> dict:
        """Serialize HarnessState for JSON storage."""
        return {
            "messages": self._serialize_messages(state.get("messages", [])),
            "step_count": state.get("step_count", 0),
            "approved": state.get("approved", True),
        }


# ──────────────────────────────────────────────────
# Global accessor
# ──────────────────────────────────────────────────

def get_session_manager() -> SessionManager:
    """Get the global SessionManager instance."""
    return SessionManager()