# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AsyncpgAdapter sets tenant context before pooled SQL under RLS."""

from __future__ import annotations

from typing import Any

import pytest

from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter
from omnimarket.config.settings import Settings
from omnimarket.projection import tenant_isolation as tenant_isolation_module
from omnimarket.projection.tenant_isolation import TENANT_GUC


class _Tx:
    async def __aenter__(self) -> _Tx:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class _Acquire:
    def __init__(self, conn: _Conn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _Conn:
        return self._conn

    async def __aexit__(self, *exc: object) -> None:
        return None


class _Conn:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def transaction(self) -> _Tx:
        return _Tx()

    async def execute(self, query: str, *params: Any) -> str:
        self.calls.append((query, params))
        return "OK"

    async def fetch(self, query: str, *params: Any) -> list[dict[str, object]]:
        self.calls.append((query, params))
        return [{"ok": True}]

    async def fetchval(self, query: str, *params: Any) -> object:
        self.calls.append((query, params))
        return "value"

    async def executemany(self, query: str, params_list: list[tuple[Any, ...]]) -> None:
        self.calls.append((query, (params_list,)))


class _Pool:
    def __init__(self, conn: _Conn) -> None:
        self._conn = conn

    def acquire(self) -> _Acquire:
        return _Acquire(self._conn)


@pytest.fixture
def adapter(monkeypatch: pytest.MonkeyPatch) -> tuple[AsyncpgAdapter, _Conn]:
    monkeypatch.setattr(
        tenant_isolation_module,
        "get_settings",
        lambda: Settings(enforce_tenant_isolation=False, onex_tenant_id="tenant-a"),
    )
    db = AsyncpgAdapter(dsn="postgres://example.invalid/db")
    conn = _Conn()
    db._pool = _Pool(conn)  # type: ignore[assignment]
    return db, conn


@pytest.mark.asyncio
async def test_execute_sets_tenant_context_before_query(
    adapter: tuple[AsyncpgAdapter, _Conn],
) -> None:
    db, conn = adapter

    rows = await db.execute("SELECT * FROM context_roi_scores WHERE id = $1", "r1")

    assert rows == [{"ok": True}]
    assert conn.calls == [
        ("SELECT set_config($1, $2, true)", (TENANT_GUC, "tenant-a")),
        ("SELECT * FROM context_roi_scores WHERE id = $1", ("r1",)),
    ]


@pytest.mark.asyncio
async def test_fetchval_sets_tenant_context_before_query(
    adapter: tuple[AsyncpgAdapter, _Conn],
) -> None:
    db, conn = adapter

    value = await db.fetchval("SELECT count(*) FROM context_roi_scores")

    assert value == "value"
    assert conn.calls[0] == (
        "SELECT set_config($1, $2, true)",
        (TENANT_GUC, "tenant-a"),
    )


@pytest.mark.asyncio
async def test_execute_many_sets_tenant_context_before_batch(
    adapter: tuple[AsyncpgAdapter, _Conn],
) -> None:
    db, conn = adapter

    await db.execute_many("INSERT INTO context_roi_scores(id) VALUES($1)", [("r1",)])

    assert conn.calls == [
        ("SELECT set_config($1, $2, true)", (TENANT_GUC, "tenant-a")),
        ("INSERT INTO context_roi_scores(id) VALUES($1)", ([("r1",)],)),
    ]


@pytest.mark.asyncio
async def test_execute_in_transaction_sets_tenant_context_before_batch(
    adapter: tuple[AsyncpgAdapter, _Conn],
) -> None:
    db, conn = adapter

    await db.execute_in_transaction(
        [("INSERT INTO context_roi_scores(id) VALUES($1)", ("r1",))]
    )

    assert conn.calls == [
        ("SELECT set_config($1, $2, true)", (TENANT_GUC, "tenant-a")),
        ("INSERT INTO context_roi_scores(id) VALUES($1)", ("r1",)),
    ]
