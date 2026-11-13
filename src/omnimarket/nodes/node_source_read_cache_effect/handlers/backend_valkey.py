# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A sync adapter over the async BackendCacheRedis."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

from omnibase_core.backends.cache import BackendCacheRedis
from omnibase_core.errors.model_onex_error import ModelOnexError

from omnimarket.nodes.node_source_read_cache_effect.protocols.protocol_source_read_cache_backend import (
    SourceCacheUnavailableError,
)


def valkey_url(host: str, port: int, password: str | None, database: int) -> str:
    if password:
        return f"redis://:{quote(password, safe='')}@{host}:{port}/{database}"
    return f"redis://{host}:{port}/{database}"


class BackendValkeySourceReadCache:
    """A sync adapter over BackendCacheRedis; owns one private event loop."""

    def __init__(
        self,
        url: str,
        connect_timeout_seconds: int,
        operation_timeout_seconds: int,
        *,
        backend_factory: Callable[[str], Any] | None = None,
    ) -> None:
        self._url = url
        self._connect_timeout = connect_timeout_seconds
        self._operation_timeout = operation_timeout_seconds
        self._backend_factory = backend_factory
        self._loop = asyncio.new_event_loop()
        self._backend: Any = None
        self._connected = False
        self._unavailable = False
        self._closed = False

    def _ensure_connected(self) -> None:
        if self._unavailable:
            raise SourceCacheUnavailableError("backend unavailable")
        if self._connected:
            return
        if self._backend is None:
            factory = self._backend_factory or BackendCacheRedis
            self._backend = factory(self._url)
        try:
            self._loop.run_until_complete(
                asyncio.wait_for(self._backend.connect(), self._connect_timeout)
            )
            self._connected = True
        except (ModelOnexError, OSError, TimeoutError):
            self._unavailable = True
            raise SourceCacheUnavailableError("backend unavailable") from None

    def get(self, key: str) -> str | None:
        self._ensure_connected()
        try:
            found = self._loop.run_until_complete(
                asyncio.wait_for(self._backend.get(key), self._operation_timeout)
            )
        except (TimeoutError, ModelOnexError, OSError, ValueError, RuntimeError):
            self._unavailable = True
            raise SourceCacheUnavailableError("backend unavailable") from None
        return found if isinstance(found, str) else None

    def put(self, key: str, value: str, ttl_seconds: int) -> None:
        self._ensure_connected()
        try:
            self._loop.run_until_complete(
                asyncio.wait_for(
                    self._backend.set(key, value, ttl_seconds=ttl_seconds),
                    self._operation_timeout,
                )
            )
        except (TimeoutError, ModelOnexError, OSError, ValueError, RuntimeError):
            self._unavailable = True
            raise SourceCacheUnavailableError("backend unavailable") from None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._backend is not None:
            try:
                self._loop.run_until_complete(self._backend.close())
            except (ModelOnexError, OSError, RuntimeError):
                # closing a connection that is already gone must not fail the step that read through it
                self._backend = None
        self._loop.close()
