# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Helpers of the source read cache tests: a controllable clock, a backend that fails, and a reader over the in-memory backend."""

from __future__ import annotations

from omnimarket.models.model_source_read_cache import (
    ModelSourceCacheKey,
)
from omnimarket.nodes.node_source_read_cache_effect.handlers.backend_memory import (
    BackendMemorySourceReadCache,
)
from omnimarket.nodes.node_source_read_cache_effect.handlers.cached_source_reader import (
    CachedSourceReader,
)
from omnimarket.nodes.node_source_read_cache_effect.handlers.handler_source_read_cache import (
    HandlerSourceReadCache,
)
from omnimarket.nodes.node_source_read_cache_effect.protocols.protocol_source_read_cache_backend import (
    SourceCacheUnavailableError,
)

PREFIX = "test-board:"
TTL_BACKSTOP = 900


class Clock:
    """A clock the test moves."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class DownBackend:
    """A backend that cannot be reached, as a stopped server is."""

    def get(self, key: str) -> str | None:
        raise SourceCacheUnavailableError("down")

    def put(self, key: str, value: str, ttl_seconds: int) -> None:
        raise SourceCacheUnavailableError("down")

    def close(self) -> None:
        return None


CLONE_SETTINGS = ("{name}", 5)


def memory_reader(
    clock: Clock | None = None,
) -> tuple[CachedSourceReader, BackendMemorySourceReadCache]:
    ticks = clock or Clock()
    backend = BackendMemorySourceReadCache(ticks)
    handler = HandlerSourceReadCache(backend, PREFIX, TTL_BACKSTOP)
    return (
        CachedSourceReader(
            handler, ticks, backend_name="memory", clone_settings=CLONE_SETTINGS
        ),
        backend,
    )


def key(revision: str = "r1", source: str = "src") -> ModelSourceCacheKey:
    return ModelSourceCacheKey(
        source_id=source,
        params_digest=ModelSourceCacheKey.digest({"limit": 5}),
        revision=revision,
    )
