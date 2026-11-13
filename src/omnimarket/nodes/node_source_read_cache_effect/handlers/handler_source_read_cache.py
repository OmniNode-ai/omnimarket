# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The canonical handler for the source read cache effect."""

from __future__ import annotations

from omnimarket.nodes.node_source_read_cache_effect.models.model_source_read_cache import (
    EnumSourceCacheOutcome,
    ModelSourceCacheRequest,
    ModelSourceCacheResult,
)
from omnimarket.nodes.node_source_read_cache_effect.protocols.protocol_source_read_cache_backend import (
    ProtocolSourceReadCacheBackend,
    SourceCacheUnavailableError,
)


class HandlerSourceReadCache:
    """Answers a lookup or a store against the backend; a None backend answers BYPASS."""

    def __init__(
        self,
        backend: ProtocolSourceReadCacheBackend | None,
        key_prefix: str,
        ttl_backstop_seconds: int,
    ) -> None:
        self._backend = backend
        self._key_prefix = key_prefix
        self._ttl_backstop = ttl_backstop_seconds

    def handle(self, request: ModelSourceCacheRequest) -> ModelSourceCacheResult:
        if self._backend is None:
            return ModelSourceCacheResult(
                outcome=EnumSourceCacheOutcome.BYPASS, reason="no backend"
            )
        rendered = request.key.render(self._key_prefix)
        if request.operation == "lookup":
            try:
                payload = self._backend.get(rendered)
            except SourceCacheUnavailableError:
                return ModelSourceCacheResult(
                    outcome=EnumSourceCacheOutcome.UNAVAILABLE,
                    reason="backend unavailable",
                )
            if payload is not None:
                return ModelSourceCacheResult(
                    outcome=EnumSourceCacheOutcome.HIT, payload=payload
                )
            return ModelSourceCacheResult(outcome=EnumSourceCacheOutcome.MISS)
        # store (the request model refuses a store with no payload)
        ttl = request.ttl_seconds or self._ttl_backstop
        try:
            self._backend.put(rendered, request.payload or "", ttl)
        except SourceCacheUnavailableError:
            return ModelSourceCacheResult(
                outcome=EnumSourceCacheOutcome.UNAVAILABLE, reason="backend unavailable"
            )
        return ModelSourceCacheResult(outcome=EnumSourceCacheOutcome.STORED)

    def close(self) -> None:
        if self._backend is not None:
            self._backend.close()
