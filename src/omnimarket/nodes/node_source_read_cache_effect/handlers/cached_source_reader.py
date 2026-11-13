# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The in-process reader the board's steps call: a source read that goes to its source only when the cache has no current answer.

``read`` asks the handler for the key (source id, digest of the read's parameters, the source's revision). A hit that decodes and that
the caller still judges usable is returned and the source is not read; anything else reads the source, counts the read, and stores the
answer when the caller says it is worth keeping. A backend that cannot be reached, a payload that cannot be decoded and a handler that is
absent each fall through to the source, so the cache can only make a step faster, never make it fail.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from pydantic import ValidationError

from omnimarket.nodes.node_source_read_cache_effect.handlers.handler_source_read_cache import (
    HandlerSourceReadCache,
)
from omnimarket.nodes.node_source_read_cache_effect.models.model_source_read_cache import (
    EnumSourceCacheOutcome,
    ModelSourceCacheKey,
    ModelSourceCacheRequest,
    ModelSourceCacheTally,
)


class CachedSourceReader:
    """Reads a source through the cache and keeps the tally of what the cache did."""

    def __init__(
        self,
        handler: HandlerSourceReadCache | None,
        clock: Callable[[], float] = time.monotonic,
        disabled_reason: str = "",
        backend_name: str = "none",
        clone_settings: tuple[str, int] | None = None,
    ) -> None:
        self._handler = handler
        self._clone_settings = clone_settings
        self._clock = clock
        self.tally = ModelSourceCacheTally(
            backend=backend_name, disabled_reason=disabled_reason
        )

    def _fetch[T](self, fetch: Callable[[], T]) -> T:
        started = self._clock()
        value = fetch()
        self.tally.source_reads += 1
        self.tally.source_seconds = round(
            self.tally.source_seconds + (self._clock() - started), 3
        )
        return value

    def lookup[T](
        self,
        key: ModelSourceCacheKey,
        decode: Callable[[str], T],
        *,
        usable: Callable[[T], bool] = lambda _value: True,
    ) -> T | None:
        """The cached value for ``key`` when the cache holds one the caller still accepts, else ``None`` (counted as a hit, miss or bypass)."""
        if self._handler is None:
            self.tally.bypassed += 1
            return None
        looked = self._handler.handle(
            ModelSourceCacheRequest(operation="lookup", key=key)
        )
        if looked.outcome == EnumSourceCacheOutcome.UNAVAILABLE:
            self.tally.unavailable += 1
            return None
        if looked.outcome == EnumSourceCacheOutcome.HIT and looked.payload is not None:
            try:
                cached = decode(looked.payload)
            except (ValueError, ValidationError):
                self.tally.misses += 1
                return None
            if usable(cached):
                self.tally.hits += 1
                return cached
        self.tally.misses += 1
        return None

    def store[T](
        self, key: ModelSourceCacheKey, value: T, encode: Callable[[T], str]
    ) -> None:
        """Keep ``value`` under ``key`` (counted as a store, or as unavailable); a handler that is absent keeps nothing."""
        if self._handler is None:
            return
        stored = self._handler.handle(
            ModelSourceCacheRequest(operation="store", key=key, payload=encode(value))
        )
        if stored.outcome == EnumSourceCacheOutcome.STORED:
            self.tally.stores += 1
        elif stored.outcome == EnumSourceCacheOutcome.UNAVAILABLE:
            self.tally.unavailable += 1

    def read[T](
        self,
        key: ModelSourceCacheKey,
        fetch: Callable[[], T],
        encode: Callable[[T], str],
        decode: Callable[[str], T],
        *,
        usable: Callable[[T], bool] = lambda _value: True,
        store_if: Callable[[T], bool] = lambda _value: True,
    ) -> tuple[T, EnumSourceCacheOutcome]:
        """The cached value for ``key``, or the source's answer (stored when ``store_if`` says so)."""
        if self._handler is None:
            self.tally.bypassed += 1
            return self._fetch(fetch), EnumSourceCacheOutcome.BYPASS
        unavailable_before = self.tally.unavailable
        cached = self.lookup(key, decode, usable=usable)
        if cached is not None:
            return cached, EnumSourceCacheOutcome.HIT
        reachable = self.tally.unavailable == unavailable_before
        value = self._fetch(fetch)
        if reachable and store_if(value):
            self.store(key, value, encode)
        return value, (
            EnumSourceCacheOutcome.MISS
            if reachable
            else EnumSourceCacheOutcome.UNAVAILABLE
        )

    @property
    def clone_revision_settings(self) -> tuple[str, int] | None:
        """The overlay's clone location template and timeout, or ``None`` when no cache is configured."""
        return self._clone_settings

    def close(self) -> None:
        if self._handler is not None:
            self._handler.close()
