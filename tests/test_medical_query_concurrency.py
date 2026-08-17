import asyncio

import pytest

from microharness.medical.query_concurrency import (
    MedicalQueryCoordinator,
    MedicalQueryDuplicateId,
    MedicalQueryQueueFull,
    MedicalQueryQueueTimeout,
)


def test_coordinator_admits_in_fifo_order_and_reports_positions():
    async def scenario():
        coordinator = MedicalQueryCoordinator(
            max_concurrency=1,
            max_queue=2,
            queue_timeout_seconds=1,
        )
        await coordinator.acquire("first")
        second = asyncio.create_task(coordinator.acquire("second"))
        third = asyncio.create_task(coordinator.acquire("third"))
        await asyncio.sleep(0)

        assert coordinator.snapshot("second")["queue_position"] == 1
        assert coordinator.snapshot("third")["queue_position"] == 2

        coordinator.release("first")
        assert (await asyncio.wait_for(second, 0.2))["status"] == "running"
        assert not third.done()

        coordinator.release("second")
        assert (await asyncio.wait_for(third, 0.2))["status"] == "running"
        coordinator.release("third")
        assert coordinator.snapshot()["active_count"] == 0

    asyncio.run(scenario())


def test_coordinator_rejects_when_bounded_queue_is_full():
    async def scenario():
        coordinator = MedicalQueryCoordinator(1, 1, 1)
        await coordinator.acquire("running")
        queued = asyncio.create_task(coordinator.acquire("queued"))
        await asyncio.sleep(0)

        with pytest.raises(MedicalQueryQueueFull):
            await coordinator.acquire("rejected")

        assert coordinator.snapshot("rejected")["status"] == "rejected"
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        coordinator.release("running")

    asyncio.run(scenario())


def test_coordinator_times_out_waiting_without_leaking_slot():
    async def scenario():
        coordinator = MedicalQueryCoordinator(1, 1, 0.1)
        await coordinator.acquire("running")

        with pytest.raises(MedicalQueryQueueTimeout):
            await coordinator.acquire("timed-out")

        assert coordinator.snapshot("timed-out")["status"] == "queue_timeout"
        assert coordinator.snapshot()["queue_length"] == 0
        assert coordinator.snapshot()["active_count"] == 1
        coordinator.release("running")

    asyncio.run(scenario())


def test_coordinator_rejects_duplicate_active_request_id():
    async def scenario():
        coordinator = MedicalQueryCoordinator(1, 1, 1)
        await coordinator.acquire("same-id")
        with pytest.raises(MedicalQueryDuplicateId):
            await coordinator.acquire("same-id")
        coordinator.release("same-id")

    asyncio.run(scenario())


def test_coordinator_loads_limits_from_environment(monkeypatch):
    monkeypatch.setenv("MEDICAL_QUERY_MAX_CONCURRENCY", "3")
    monkeypatch.setenv("MEDICAL_QUERY_MAX_QUEUE", "7")
    monkeypatch.setenv("MEDICAL_QUERY_QUEUE_TIMEOUT_SECONDS", "45")

    coordinator = MedicalQueryCoordinator.from_env()

    assert coordinator.max_concurrency == 3
    assert coordinator.max_queue == 7
    assert coordinator.queue_timeout_seconds == 45
