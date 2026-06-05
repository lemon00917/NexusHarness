"""Test token_tracker memory boundedness."""
import time


def test_max_calls_limit():
    """Test that record() trims oldest calls when MAX_CALLS exceeded."""
    from microharness.observability.token_tracker import TokenTracker

    # Use small limit for fast testing
    tracker = TokenTracker(max_calls=100)

    # Record many calls
    for i in range(200):
        tracker.record("test", f"model_{i % 10}", input_tokens=100, output_tokens=50)

    # Should be trimmed to MAX_CALLS
    assert len(tracker.calls) == 100, f"Expected 100, got {len(tracker.calls)}"
    assert tracker.call_count == 100

    # Oldest records (model_0 to model_9) should be trimmed, newest (model_190+) kept
    recent_models = {r.model for r in tracker.calls}
    assert "model_0" not in recent_models, "Oldest calls should be trimmed"
    assert "model_9" in recent_models, "Recent calls should be kept"

    print("✓ max_calls_limit passed")


def test_unbounded_growth_prevention():
    """Test that repeated recording stays bounded."""
    from microharness.observability.token_tracker import TokenTracker

    tracker = TokenTracker(max_calls=100)

    # Continuously record over many iterations
    for batch in range(10):
        for i in range(50):
            tracker.record("provider", "model", input_tokens=100, output_tokens=50)
        # Size should never exceed MAX_CALLS
        assert len(tracker.calls) <= tracker.MAX_CALLS, \
            f"Exceeded limit: {len(tracker.calls)} > {tracker.MAX_CALLS}"

    assert len(tracker.calls) <= 100
    print(f"✓ unbounded_growth_prevention passed (final size: {len(tracker.calls)})")


def test_merge_trims_to_limit():
    """Test that merge() also respects MAX_CALLS."""
    from microharness.observability.token_tracker import TokenTracker

    tracker1 = TokenTracker(max_calls=100)
    tracker2 = TokenTracker(max_calls=100)

    # Fill both
    for i in range(100):
        tracker1.record("p", "m", 100, 50)
    for i in range(100):
        tracker2.record("p", "m", 100, 50)

    # Merge
    tracker1.merge(tracker2)

    # Should be trimmed
    assert len(tracker1.calls) == 100, f"Expected 100, got {len(tracker1.calls)}"
    print("✓ merge_trims_to_limit passed")


def test_summary_with_bounded_calls():
    """Test that get_summary works correctly after trimming."""
    from microharness.observability.token_tracker import TokenTracker

    tracker = TokenTracker(max_calls=10)

    # Record some calls
    for i in range(20):
        tracker.record("openai", "gpt-4o", input_tokens=1000, output_tokens=500)

    summary = tracker.get_summary()

    # Should reflect 10 records, not 20
    assert summary["total_calls"] == 10, f"Expected 10, got {summary['total_calls']}"
    assert summary["total_tokens"] == 10 * 1500
    assert summary["last_call"] is not None
    assert summary["first_call"] is not None

    print("✓ summary_with_bounded_calls passed")


def test_history_respects_limit():
    """Test get_history respects the actual bounded size."""
    from microharness.observability.token_tracker import TokenTracker

    tracker = TokenTracker(max_calls=10)

    for i in range(50):
        tracker.record("p", "m", 100, 50)

    # History should return at most MAX_CALLS
    history = tracker.get_history()
    assert len(history) <= 10, f"History exceeds limit: {len(history)}"

    print("✓ history_respects_limit passed")


if __name__ == "__main__":
    test_max_calls_limit()
    test_unbounded_growth_prevention()
    test_merge_trims_to_limit()
    test_summary_with_bounded_calls()
    test_history_respects_limit()
    print("\nAll token_tracker tests passed!")