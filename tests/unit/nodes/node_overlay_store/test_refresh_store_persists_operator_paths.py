# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Postgres store keeps the settings an operator set with the version, and reads them back (OMN-26495)."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any

import pytest

from omnimarket.handlers.overlay_store.store_postgres import PostgresOverlayStore
from tests.unit.nodes.node_overlay_store.support import NOW, record

pytestmark = pytest.mark.unit

PATHS = ("hosts/[name=h1]/cap",)


class _Connection:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self.executed: list[tuple[str, tuple[Any, ...]]] = []
        self._row = row

    async def execute(self, sql: str, *args: Any) -> None:
        self.executed.append((sql, args))

    async def fetchval(self, sql: str, *args: Any) -> None:
        return None

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        return self._row

    @asynccontextmanager
    async def transaction(self) -> Any:
        yield


class _Pool:
    def __init__(self, connection: _Connection) -> None:
        self._connection = connection

    @asynccontextmanager
    async def acquire(self) -> Any:
        yield self._connection


@pytest.mark.asyncio
async def test_operator_paths_are_written_with_the_version() -> None:
    connection = _Connection(None)
    written = await PostgresOverlayStore(_Pool(connection)).write_version(
        record(operator_paths=PATHS).model_dump(mode="json")
    )
    assert written
    args = next(args for sql, args in connection.executed if "operator_paths" in sql)
    assert json.loads(args[-1]) == list(PATHS)


@pytest.mark.asyncio
@pytest.mark.parametrize("held", [json.dumps(list(PATHS)), None])
async def test_operator_paths_are_read_back_and_a_row_without_them_reads_as_none(
    held: str | None,
) -> None:
    stored = record()
    row = {
        "overlay_id": stored.overlay_id,
        "digest": stored.digest,
        "document": json.dumps(stored.document),
        "source_ref": stored.source_ref,
        "loaded_by": stored.loaded_by,
        "last_loaded_at": NOW,
        "revision": 1,
        "operator_paths": held,
    }
    current = await PostgresOverlayStore(_Pool(_Connection(row))).read_current(
        "lab_hosts"
    )
    assert current is not None
    assert current["operator_paths"] == (list(PATHS) if held else [])
