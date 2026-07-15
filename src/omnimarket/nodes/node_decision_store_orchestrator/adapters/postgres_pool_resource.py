# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pool-resource boundary for the decision_store Postgres adapter."""

from __future__ import annotations

from types import TracebackType
from typing import Any

from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter


class AsyncpgPoolResource:
    """Context-managed asyncpg pool adapter resource.

    The live orchestrator adapter receives this as a factory boundary so the
    adapter does not directly construct DB/cache connections in its handler path.
    """

    def __init__(self, dsn: str) -> None:
        self._db = AsyncpgAdapter(dsn=dsn, min_size=1, max_size=2)

    async def __aenter__(self) -> Any:
        await self._db.connect()
        return self._db.pool

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self._db.close()


def default_pool_resource(dsn: str) -> AsyncpgPoolResource:
    return AsyncpgPoolResource(dsn)
