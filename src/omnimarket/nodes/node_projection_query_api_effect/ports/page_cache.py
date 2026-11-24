# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Valkey-backed page cache of the projection query node (OMN-24458).

A served projection page is held in the shared Valkey for the expiry the node
contract's ``page_cache`` block declares (``ttl_seconds``); this module holds no
number of its own. A read past the expiry finds nothing and the handler
recomputes the page from the windowed store read.

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
from collections.abc import Callable, Mapping
from typing import Any, Protocol
from urllib.parse import quote

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

    async def get(self, key: str) -> dict[str, Any] | None: ...

    async def put(self, key: str, page: dict[str, Any]) -> None: ...

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
    ) -> None:
        self._settings = settings
        self._client = client

    @property
    def ttl_seconds(self) -> int:
        return self._settings.ttl_seconds

    async def get(self, key: str) -> dict[str, Any] | None:
        timeout = self._settings.valkey.operation_timeout_seconds
        try:
            raw = await asyncio.wait_for(self._client.get(key), timeout)
        except Exception as exc:
            raise PageCacheUnavailableError(type(exc).__name__) from None
        if raw is None:
            return None
        try:
            page = json.loads(raw)
        except (TypeError, ValueError):
            return None
        return page if isinstance(page, dict) else None

    async def put(self, key: str, page: dict[str, Any]) -> None:
        timeout = self._settings.valkey.operation_timeout_seconds
        try:
            await asyncio.wait_for(
                self._client.set(
                    key, json.dumps(page, default=str), ex=self._settings.ttl_seconds
                ),
                timeout,
            )
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
