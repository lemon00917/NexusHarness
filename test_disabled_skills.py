"""Test disabled_skills module thread safety and basic operations."""
import threading
import time


def test_basic_add_remove():
    """Test basic add/remove operations."""
    from microharness.agent.disabled_skills import (
        add_disabled_skill,
        remove_disabled_skill,
        get_disabled_skills,
        reset_disabled,
    )

    reset_disabled()

    add_disabled_skill("skill_a")
    add_disabled_skill("skill_b")

    disabled = get_disabled_skills()
    assert "skill_a" in disabled, f"Expected skill_a in {disabled}"
    assert "skill_b" in disabled

    remove_disabled_skill("skill_a")
    disabled = get_disabled_skills()
    assert "skill_a" not in disabled
    assert "skill_b" in disabled

    reset_disabled()
    disabled = get_disabled_skills()
    assert len(disabled) == 0

    print("✓ basic_add_remove passed")


def test_thread_safety():
    """Test thread-safe access to disabled_skills."""
    from microharness.agent.disabled_skills import (
        add_disabled_skill,
        get_disabled_skills,
        reset_disabled,
    )

    reset_disabled()

    errors = []
    results = []

    def worker(skill_id):
        try:
            for i in range(100):
                name = f"skill_{skill_id}"
                add_disabled_skill(name)
                time.sleep(0.001)
                disabled = get_disabled_skills()
                results.append(len(disabled))
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(errors) == 0, f"Thread errors: {errors}"
    print(f"✓ thread_safety passed (max concurrent: {max(results)}, no errors)")


def test_is_disabled():
    """Test is_disabled checker."""
    from microharness.agent.disabled_skills import (
        is_disabled,
        add_disabled_skill,
        remove_disabled_skill,
        reset_disabled,
    )

    reset_disabled()

    assert not is_disabled("test_skill")
    add_disabled_skill("test_skill")
    assert is_disabled("test_skill")
    remove_disabled_skill("test_skill")
    assert not is_disabled("test_skill")

    reset_disabled()
    print("✓ is_disabled passed")


if __name__ == "__main__":
    test_basic_add_remove()
    test_is_disabled()
    test_thread_safety()
    print("\nAll disabled_skills tests passed!")