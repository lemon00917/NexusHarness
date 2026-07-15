"""Bounded, observable concurrency control for medical-filter queries."""

from __future__ import annotations

import asyncio
import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, Optional


class MedicalQueryQueueFull(Exception):
    """Raised when no more medical-filter requests can be queued."""


class MedicalQueryQueueTimeout(Exception):
    """Raised when a request waits too long for an execution slot."""


class MedicalQueryDuplicateId(Exception):
    """Raised when an active request reuses an existing request ID."""


@dataclass
class _Waiter:
    request_id: str
    future: asyncio.Future
    loop: asyncio.AbstractEventLoop


def _env_int(name: str, default: int, minimum: int) -> int:
    try:
        return max(minimum, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float, minimum: float) -> float:
    try:
        return max(minimum, float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


class MedicalQueryCoordinator:
    """FIFO admission controller that keeps executor submission bounded."""

    def __init__(
        self,
        max_concurrency: int = 4,
        max_queue: int = 20,
        queue_timeout_seconds: float = 300.0,
        state_ttl_seconds: float = 600.0,
    ) -> None:
        self.max_concurrency = max(1, int(max_concurrency))
        self.max_queue = max(0, int(max_queue))
        self.queue_timeout_seconds = max(0.1, float(queue_timeout_seconds))
        self.state_ttl_seconds = max(60.0, float(state_ttl_seconds))
        self._lock = threading.RLock()
        self._active: set[str] = set()
        self._waiting: Deque[_Waiter] = deque()
        self._states: Dict[str, dict] = {}

    @classmethod
    def from_env(cls) -> "MedicalQueryCoordinator":
        return cls(
            max_concurrency=_env_int("MEDICAL_QUERY_MAX_CONCURRENCY", 4, 1),
            max_queue=_env_int("MEDICAL_QUERY_MAX_QUEUE", 20, 0),
            queue_timeout_seconds=_env_float(
                "MEDICAL_QUERY_QUEUE_TIMEOUT_SECONDS", 300.0, 0.1
            ),
        )

    def _cleanup_locked(self, now: Optional[float] = None) -> None:
        now = now or time.time()
        removable = [
            request_id
            for request_id, state in self._states.items()
            if state.get("finished_at")
            and now - state["finished_at"] > self.state_ttl_seconds
        ]
        for request_id in removable:
            self._states.pop(request_id, None)

    def _state_locked(self, request_id: str, status: str) -> dict:
        now = time.time()
        state = {
            "request_id": request_id,
            "status": status,
            "submitted_at": now,
            "started_at": now if status == "running" else None,
            "finished_at": None,
            "message": "running" if status == "running" else "waiting",
        }
        self._states[request_id] = state
        return state

    async def acquire(self, request_id: str) -> dict:
        loop = asyncio.get_running_loop()
        waiter: Optional[_Waiter] = None

        with self._lock:
            self._cleanup_locked()
            existing = self._states.get(request_id)
            if existing and existing.get("status") in {"waiting", "running"}:
                raise MedicalQueryDuplicateId(request_id)

            if len(self._active) < self.max_concurrency and not self._waiting:
                self._active.add(request_id)
                self._state_locked(request_id, "running")
                return self.snapshot(request_id)

            if len(self._waiting) >= self.max_queue:
                state = self._state_locked(request_id, "rejected")
                state.update(finished_at=time.time(), message="queue full")
                raise MedicalQueryQueueFull(request_id)

            future = loop.create_future()
            waiter = _Waiter(request_id=request_id, future=future, loop=loop)
            self._waiting.append(waiter)
            self._state_locked(request_id, "waiting")

        try:
            await asyncio.wait_for(
                asyncio.shield(waiter.future),
                timeout=self.queue_timeout_seconds,
            )
            return self.snapshot(request_id)
        except asyncio.TimeoutError as exc:
            if self._remove_waiter(request_id):
                self._finish_state(request_id, "queue_timeout", "queue wait timeout")
                raise MedicalQueryQueueTimeout(request_id) from exc
            # Admission won the race with timeout; the caller owns the slot.
            return self.snapshot(request_id)
        except asyncio.CancelledError:
            if self._remove_waiter(request_id):
                self._finish_state(request_id, "cancelled", "request cancelled")
            elif self.is_active(request_id):
                self.release(request_id, "cancelled", "request cancelled")
            raise

    def is_active(self, request_id: str) -> bool:
        with self._lock:
            return request_id in self._active

    def _remove_waiter(self, request_id: str) -> bool:
        with self._lock:
            for waiter in tuple(self._waiting):
                if waiter.request_id == request_id:
                    self._waiting.remove(waiter)
                    return True
        return False

    def _finish_state(self, request_id: str, status: str, message: str) -> None:
        with self._lock:
            state = self._states.get(request_id) or self._state_locked(request_id, status)
            state.update(status=status, finished_at=time.time(), message=message)

    def release(
        self,
        request_id: str,
        status: str = "completed",
        message: str = "completed",
    ) -> None:
        next_waiter: Optional[_Waiter] = None
        with self._lock:
            self._active.discard(request_id)
            self._finish_state(request_id, status, message)

            while self._waiting and len(self._active) < self.max_concurrency:
                candidate = self._waiting.popleft()
                if candidate.future.cancelled():
                    continue
                next_waiter = candidate
                self._active.add(candidate.request_id)
                state = self._states[candidate.request_id]
                state.update(
                    status="running",
                    started_at=time.time(),
                    message="running",
                )
                break

        if next_waiter is not None:
            def _admit() -> None:
                if not next_waiter.future.done():
                    next_waiter.future.set_result(True)

            next_waiter.loop.call_soon_threadsafe(_admit)

    def mark_disconnected(self, request_id: str) -> None:
        with self._lock:
            state = self._states.get(request_id)
            if state and state.get("status") == "running":
                state["message"] = "client disconnected; work is still running"

    def snapshot(self, request_id: Optional[str] = None) -> dict:
        with self._lock:
            self._cleanup_locked()
            waiting_ids = [waiter.request_id for waiter in self._waiting]
            result = {
                "max_concurrency": self.max_concurrency,
                "active_count": len(self._active),
                "max_queue": self.max_queue,
                "queue_length": len(waiting_ids),
                "queue_timeout_seconds": self.queue_timeout_seconds,
            }
            if request_id is None:
                return result

            state = self._states.get(request_id)
            if state is None:
                return {**result, "request_id": request_id, "status": "not_found"}
            item = dict(state)
            item["queue_position"] = (
                waiting_ids.index(request_id) + 1 if request_id in waiting_ids else 0
            )
            return {**result, **item}
