# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Postgres kind: the projection's table, reached through a pool the wiring opens on first use."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from types import TracebackType
from typing import Any

from omnibase_spi.protocols.storage import ProtocolOverlayStore

from omnimarket.handlers.overlay_store.store_postgres import PostgresOverlayStore
from omnimarket.models.overlay_store.model_store_backend import ModelStoreBackendSpec

ADDRESS_INPUT = "ONEX_SETTINGS_STORE_ADDRESS"
PORT_INPUT = "ONEX_SETTINGS_STORE_PORT"
DATABASE_INPUT = "ONEX_SETTINGS_STORE_DATABASE"
LOGIN_INPUT = "ONEX_SETTINGS_STORE_LOGIN"
PASSWORD_SECRET = "ONEX_SETTINGS_STORE_PASSWORD"


PoolFactory = Callable[..., Awaitable[Any]]


async def _open_asyncpg_pool(**parts: Any) -> Any:
    """The production connector; the wiring gives every value, a test injects its own in place of it."""
    from asyncpg import create_pool

    return await create_pool(**parts)


class _LazyPool:
    """Opens the pool when the first query asks for a connection, so wiring itself opens nothing."""

    def __init__(self, dsn_parts: Mapping[str, Any], pool_factory: PoolFactory) -> None:
        self._parts = dsn_parts
        self._pool_factory = pool_factory
        self._pool: Any = None

    def acquire(self) -> _Acquire:
        return _Acquire(self)

    async def open_pool(self) -> Any:
        if self._pool is None:
            self._pool = await self._pool_factory(**self._parts)
        return self._pool


class _Acquire:
    def __init__(self, owner: _LazyPool) -> None:
        self._owner = owner
        self._inner: Any = None

    async def __aenter__(self) -> Any:
        pool = await self._owner.open_pool()
        self._inner = pool.acquire()
        return await self._inner.__aenter__()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self._inner.__aexit__(exc_type, exc, tb)


class BackendPostgres:
    spec = ModelStoreBackendSpec(
        kind="store_postgres",
        inputs=(ADDRESS_INPUT, PORT_INPUT, DATABASE_INPUT, LOGIN_INPUT),
        secrets=(PASSWORD_SECRET,),
    )

    def __init__(self, pool_factory: PoolFactory = _open_asyncpg_pool) -> None:
        self._pool_factory = pool_factory

    def build(
        self, inputs: Mapping[str, str], secrets: Mapping[str, str]
    ) -> ProtocolOverlayStore:
        pool = _LazyPool(
            {
                "host": inputs[ADDRESS_INPUT],
                "port": int(inputs[PORT_INPUT]),
                "database": inputs[DATABASE_INPUT],
                "user": inputs[LOGIN_INPUT],
                "password": secrets[PASSWORD_SECRET],
            },
            self._pool_factory,
        )
        return PostgresOverlayStore(pool)


__all__ = ["BackendPostgres"]
