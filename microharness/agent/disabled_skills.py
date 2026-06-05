"""
Disabled Skills State
==================
Simple module to track disabled skills/tools.
Avoids circular imports between web.app and prompts.py.
"""

from threading import Lock

# Thread-safe set for disabled skills
_disabled_skills: set = set()
_lock = Lock()


def get_disabled_skills() -> set:
    """Get current set of disabled skill names."""
    with _lock:
        return _disabled_skills.copy()


def add_disabled_skill(name: str) -> None:
    """Add a skill to disabled set."""
    with _lock:
        _disabled_skills.add(name)


def remove_disabled_skill(name: str) -> None:
    """Remove a skill from disabled set."""
    with _lock:
        _disabled_skills.discard(name)


def is_disabled(name: str) -> bool:
    """Check if a skill is disabled."""
    with _lock:
        return name in _disabled_skills


def reset_disabled() -> set:
    """Reset all disabled skills, return previous set."""
    with _lock:
        old = _disabled_skills.copy()
        _disabled_skills.clear()
        return old
