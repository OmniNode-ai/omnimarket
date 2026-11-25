# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Valkey-backed page cache of the projection query node (OMN-24458).

A served projection page is held in the shared Valkey. The node contract's
``page_cache`` block declares how long it is fresh (``ttl_seconds``), how long
past that it may still be served while it is refreshed (``stale_seconds``) and
how long one runtime's claim to refresh it lasts (``refresh_lock_seconds``);
this module holds no number of its own. The Valkey drops a page after
``ttl_seconds + stale_seconds``; a read after that finds nothing and the handler
recomputes the page from the windowed store read.

The held value carries the time its writer stored it, so the age of a page is
read from the page and not from a clock of the reader's own; that clock is
injected so a test can step it.

A Valkey that cannot be reached, or that answers an error or does not answer in
time, raises :class:`PageCacheUnavailableError`; the handler then serves the
windowed store read and says so in the answer. Nothing here returns an empty
page or a made-up one for a failed read.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from collections.abc import Callable, Mapping
from typing import Any, Protocol
from urllib.parse import quote

from omnimarket.nodes.node_projection_query_api_effect.models.model_held_page import (
    ModelHeldPage,
)
from omnimarket.nodes.node_projection_query_api_effect.models.model_page_cache import (
    ModelPageCacheSettings,
)
from omnimarket.nodes.node_projection_query_api_effect.models.model_projection_query_request import (
    ModelProjectionQueryRequest,
)
from omnimarket.nodes.node_projection_query_api_effect.ports.contract_routes import (
    contract_page_cache,
)


class PageCacheUnavailableError(RuntimeError):
    """The cache did not answer a get or a put; the page is read from the store instead."""


class ProtocolPageCache(Protocol):
    """What the handler asks of a page cache."""

    @property
    def ttl_seconds(self) -> int: ...

    @property
    def stale_seconds(self) -> int: ...

    async def get(self, key: str) -> ModelHeldPage | None: ...

    async def put(self, key: str, page: dict[str, Any]) -> None: ...

    async def claim_refresh(self, key: str) -> bool: ...

    async def release_refresh(self, key: str) -> None: ...

    async def close(self) -> None: ...


def page_cache_key(prefix: str, request: ModelProjectionQueryRequest) -> str:
    """The key of one page: every input that changes the rows, hashed behind the prefix."""
    inputs = request.model_dump(
        include={
            "topic",
            "since",
            "window",
            "limit",
            "order",
            "order_by",
            "tenant",
        }
    )
    digest = hashlib.sha256(
        json.dumps(inputs, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return f"{prefix}:{digest}"


def valkey_url(host: str, port: int, password: str | None, database: int) -> str:
    if password:
        return f"redis://:{quote(password, safe='')}@{host}:{port}/{database}"
    return f"redis://{host}:{port}/{database}"


class ValkeyPageCache:
    """Pages held in Valkey with the contract's expiry; failures are named, never swallowed."""

    def __init__(
        self,
        settings: ModelPageCacheSettings,
        client: Any,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._settings = settings
        self._client = client
        self._clock = clock

    @property
    def ttl_seconds(self) -> int:
        return self._settings.ttl_seconds

    @property
    def stale_seconds(self) -> int:
        return self._settings.stale_seconds

    async def get(self, key: str) -> ModelHeldPage | None:
        timeout = self._settings.valkey.operation_timeout_seconds
        try:
            raw = await asyncio.wait_for(self._client.get(key), timeout)
        except Exception as exc:
            raise PageCacheUnavailableError(type(exc).__name__) from None
        if raw is None:
            return None
        try:
            held = json.loads(raw)
        except (TypeError, ValueError):
            return None
        if not isinstance(held, dict):
            return None
        stored_at = held.get("stored_at")
        page = held.get("page")
        if (
            isinstance(stored_at, bool)
            or not isinstance(stored_at, int | float)
            or not isinstance(page, dict)
        ):
            return None
        return ModelHeldPage(page=page, age_seconds=max(0.0, self._clock() - stored_at))

    async def put(self, key: str, page: dict[str, Any]) -> None:
        timeout = self._settings.valkey.operation_timeout_seconds
        held = {"stored_at": self._clock(), "page": page}
        try:
            await asyncio.wait_for(
                self._client.set(
                    key,
                    json.dumps(held, default=str),
                    ex=self._settings.ttl_seconds + self._settings.stale_seconds,
                ),
                timeout,
            )
        except Exception as exc:
            raise PageCacheUnavailableError(type(exc).__name__) from None

    async def claim_refresh(self, key: str) -> bool:
        """Take this page's refresh claim in the shared Valkey; ``False`` when another runtime holds it."""
        timeout = self._settings.valkey.operation_timeout_seconds
        try:
            taken = await asyncio.wait_for(
                self._client.set(
                    f"{key}:refresh",
                    "1",
                    ex=self._settings.refresh_lock_seconds,
                    nx=True,
                ),
                timeout,
            )
        except Exception as exc:
            raise PageCacheUnavailableError(type(exc).__name__) from None
        return bool(taken)

    async def release_refresh(self, key: str) -> None:
        """Drop this page's refresh claim so the next expiry can be refreshed at once."""
        timeout = self._settings.valkey.operation_timeout_seconds
        try:
            await asyncio.wait_for(self._client.delete(f"{key}:refresh"), timeout)
        except Exception as exc:
            raise PageCacheUnavailableError(type(exc).__name__) from None

    async def close(self) -> None:
        close = getattr(self._client, "aclose", None)
        if close is not None:
            try:
                await close()
            except Exception:
                # closing a connection that is already gone must not fail the app's shutdown
                return


def build_page_cache(
    environ: Mapping[str, str] | None = None,
    *,
    settings: ModelPageCacheSettings | None = None,
    client_factory: Callable[[str, int, int], Any] | None = None,
) -> ValkeyPageCache | None:
    """The page cache the contract declares, or ``None`` when the environment names no Valkey host.

    Construction connects to nothing: the client opens its connection on the
    first get or put, so the app answers from its first second whatever the
    state of the Valkey.
    """
    env = os.environ if environ is None else environ
    declared = settings if settings is not None else contract_page_cache()
    valkey = declared.valkey
    host = env.get(valkey.host_env, "").strip()
    port_text = env.get(valkey.port_env, "").strip()
    if not host or not port_text.isdigit():
        return None
    password = env.get(valkey.password_env, "") if valkey.password_env else ""
    url = valkey_url(host, int(port_text), password or None, valkey.database)
    factory = client_factory or _redis_client
    return ValkeyPageCache(
        declared,
        factory(
            url,
            valkey.connect_timeout_seconds,
            valkey.operation_timeout_seconds,
        ),
    )


def _redis_client(url: str, connect_timeout: int, operation_timeout: int) -> Any:
    from redis.asyncio import Redis

    return Redis.from_url(
        url,
        socket_connect_timeout=connect_timeout,
        socket_timeout=operation_timeout,
        decode_responses=True,
    )


__all__ = [
    "PageCacheUnavailableError",
    "ProtocolPageCache",
    "ValkeyPageCache",
    "build_page_cache",
    "page_cache_key",
    "valkey_url",
]
