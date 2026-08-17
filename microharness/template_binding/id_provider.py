"""Numeric ID providers used by template-binding persistence."""

from __future__ import annotations

import hashlib
import os
import socket
import threading
import time
from typing import Protocol


class IdProviderNotConfigured(RuntimeError):
    """Raised when the configured ID provider cannot generate an ID."""


class IdProvider(Protocol):
    def next_id(self) -> int:
        """Return one globally unique positive 63-bit integer."""


class SnowflakeIdProvider:
    """Thread-safe 64-bit Snowflake generator with configurable node identity."""

    EPOCH_MS = 1704067200000
    NODE_BITS = 10
    SEQUENCE_BITS = 12
    MAX_NODE_ID = (1 << NODE_BITS) - 1
    MAX_SEQUENCE = (1 << SEQUENCE_BITS) - 1

    def __init__(self, node_id: int, *, clock_ms=None) -> None:
        if not 0 <= int(node_id) <= self.MAX_NODE_ID:
            raise IdProviderNotConfigured(
                f"snowflake node_id must be between 0 and {self.MAX_NODE_ID}"
            )
        self.node_id = int(node_id)
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._lock = threading.Lock()
        self._last_timestamp = -1
        self._sequence = 0

    def next_id(self) -> int:
        with self._lock:
            timestamp = int(self._clock_ms())
            if timestamp < self._last_timestamp:
                raise IdProviderNotConfigured(
                    "system clock moved backwards; refusing to generate a Snowflake ID"
                )
            if timestamp == self._last_timestamp:
                self._sequence = (self._sequence + 1) & self.MAX_SEQUENCE
                if self._sequence == 0:
                    timestamp = self._wait_next_millisecond(timestamp)
            else:
                self._sequence = 0
            self._last_timestamp = timestamp
            elapsed = timestamp - self.EPOCH_MS
            if elapsed < 0:
                raise IdProviderNotConfigured("system clock is earlier than the Snowflake epoch")
            return (
                (elapsed << (self.NODE_BITS + self.SEQUENCE_BITS))
                | (self.node_id << self.SEQUENCE_BITS)
                | self._sequence
            )

    def _wait_next_millisecond(self, timestamp: int) -> int:
        current = timestamp
        while current <= timestamp:
            time.sleep(0.0001)
            current = int(self._clock_ms())
        return current


def _default_node_id() -> int:
    identity = f"{socket.gethostname()}:{os.getpid()}".encode("utf-8")
    digest = hashlib.sha256(identity).digest()
    return int.from_bytes(digest[:2], "big") & SnowflakeIdProvider.MAX_NODE_ID


_provider: IdProvider | None = None
_provider_lock = threading.Lock()


def get_id_provider() -> IdProvider:
    """Return the process-wide provider.

    Production deployments should set ``TEMPLATE_BINDING_SNOWFLAKE_NODE_ID``
    uniquely per running instance. A host/process-derived value is used for
    local development so the workbench remains usable without ``MAX(id)+1``.
    """
    global _provider
    if _provider is None:
        with _provider_lock:
            if _provider is None:
                raw = os.getenv("TEMPLATE_BINDING_SNOWFLAKE_NODE_ID")
                try:
                    node_id = int(raw) if raw not in (None, "") else _default_node_id()
                except ValueError as exc:
                    raise IdProviderNotConfigured(
                        "TEMPLATE_BINDING_SNOWFLAKE_NODE_ID must be an integer"
                    ) from exc
                _provider = SnowflakeIdProvider(node_id)
    return _provider
