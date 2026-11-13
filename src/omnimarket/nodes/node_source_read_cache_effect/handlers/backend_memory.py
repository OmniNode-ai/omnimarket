# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""An in-memory source read cache backend."""

from __future__ import annotations

from collections.abc import Callable


class BackendMemorySourceReadCache:
    """A dict of key to (value, expires_at); get returns None for an absent or expired key."""

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._store: dict[str, tuple[str, float]] = {}

    def get(self, key: str) -> str | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if self._clock() >= expires_at:
            del self._store[key]
            return None
        return value

    def put(self, key: str, value: str, ttl_seconds: int) -> None:
        self._store[key] = (value, self._clock() + ttl_seconds)

    @property
    def keys(self) -> list[str]:
        now = self._clock()
        return sorted(k for k, (_, exp) in self._store.items() if now < exp)

    def close(self) -> None:
        """Nothing is held open; the store is dropped with the object."""
        self._store.clear()
