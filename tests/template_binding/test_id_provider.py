from microharness.template_binding.id_provider import IdProviderNotConfigured, SnowflakeIdProvider


def test_snowflake_ids_are_monotonic_within_same_millisecond():
    provider = SnowflakeIdProvider(7, clock_ms=lambda: 1800000000000)

    first = provider.next_id()
    second = provider.next_id()

    assert second == first + 1


def test_snowflake_rejects_clock_rollback():
    ticks = iter([1800000000001, 1800000000000])
    provider = SnowflakeIdProvider(7, clock_ms=lambda: next(ticks))
    provider.next_id()

    try:
        provider.next_id()
    except IdProviderNotConfigured as exc:
        assert "clock moved backwards" in str(exc)
    else:
        raise AssertionError("clock rollback must be rejected")
