"""Test session_manager path traversal prevention."""
import tempfile
import os


def test_safe_session_id():
    """Test _safe_session_id sanitization."""
    from microharness.memory.session_manager import _safe_session_id

    # Normal session IDs should pass through unchanged
    normal_ids = [
        "session_123456_1234",
        "my-session-001",
        "test_session_abc",
        "session_999_4567",
    ]
    for sid in normal_ids:
        assert _safe_session_id(sid) == sid, f"Normal ID should pass: {sid}"

    # Dangerous session IDs should be sanitized
    dangerous_ids = [
        ("../etc/passwd", "etcpasswd"),
        ("session_123;/bin/sh", "session_123binsh"),
        ("test`whoami`.txt", "testwhoami.txt"),
        ("$(whoami)", "whoami"),
        ("../../../etc/shadow", "etcshadow"),
        ("a|b|c", "abc"),
        ("foo && rm -rf /", "foorm-rf"),
    ]
    for original, expected in dangerous_ids:
        result = _safe_session_id(original)
        # Should not contain path separators or command chars
        assert ".." not in result, f"Path traversal not blocked: {original} -> {result}"
        assert "/" not in result, f"Forward slash not blocked: {original} -> {result}"
        assert "\\" not in result, f"Backslash not blocked: {original} -> {result}"

    print("✓ safe_session_id passed")


def test_session_lifecycle():
    """Test create/get/delete session flow."""
    from microharness.memory.session_manager import SessionManager

    # Force new instance (bypass singleton for testing)
    SessionManager._instance = None
    sm = SessionManager()

    # Create session
    state = sm.create_session("Test task for path traversal")
    assert state["session_id"] is not None
    assert state["status"] == "active"

    sid = state["session_id"]

    # Get session
    retrieved = sm.get_session(sid)
    assert retrieved is not None
    assert retrieved["session_id"] == sid

    # Delete session
    deleted = sm.delete_session(sid)
    assert deleted is True

    # Verify deleted
    gone = sm.get_session(sid)
    assert gone is None

    print("✓ session_lifecycle passed")


if __name__ == "__main__":
    test_safe_session_id()
    test_session_lifecycle()
    print("\nAll session_manager tests passed!")