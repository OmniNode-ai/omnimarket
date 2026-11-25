# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Postgres store of the outbox flush, driven through a recording pool: the statements it sends and what it returns."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, cast

import asyncpg
import pytest

from omnimarket.nodes.node_tenant_outbox_flush_effect.handlers.tenant_outbox_postgres import (
    TenantOutboxPostgres,
    TenantOutboxStoreUnavailableError,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

NOW = datetime(2026, 11, 25, 7, 0, tzinfo=UTC)


class RecordingPool:
    def __init__(
        self, rows: list[dict[str, Any]] | None = None, fail: bool = False
    ) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self._rows = rows or []
        self._fail = fail

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        if "information_schema.columns" in sql:
            self._record(sql, args)
            self.calls.pop()
            return [{"column_name": "attempts"}, {"column_name": "last_error"}]
        self._record(sql, args)
        return self._rows

    async def execute(self, sql: str, *args: Any) -> str:
        self._record(sql, args)
        return "UPDATE 1"

    def _record(self, sql: str, args: tuple[Any, ...]) -> None:
        if self._fail:
            raise asyncpg.InterfaceError("connection is closed: secret-detail")
        self.calls.append((sql, args))


def _store(pool: RecordingPool) -> TenantOutboxPostgres:
    return TenantOutboxPostgres(pool=cast(asyncpg.Pool, pool))


async def test_fetch_reads_only_unpublished_oldest_first_with_the_limit() -> None:
    envelope = {"operation": "TENANT_CREATED", "correlation_id": "c1"}
    pool = RecordingPool([{"id": "7", "envelope": json.dumps(envelope), "attempts": 2}])
    rows = await _store(pool).fetch_unpublished(5)
    sql, args = pool.calls[0]
    assert "published_at IS NULL" in sql
    assert "ORDER BY created_at, id" in sql
    assert args == (5,)
    assert [(r.row_id, r.envelope, r.attempts) for r in rows] == [("7", envelope, 2)]


async def test_fetch_accepts_a_decoded_envelope() -> None:
    pool = RecordingPool([{"id": 9, "envelope": {"k": "v"}, "attempts": 0}])
    rows = await _store(pool).fetch_unpublished(1)
    assert rows[0].row_id == "9"
    assert rows[0].envelope == {"k": "v"}


async def test_mark_published_only_touches_a_row_still_unpublished() -> None:
    pool = RecordingPool()
    await _store(pool).mark_published("7", NOW)
    sql, args = pool.calls[0]
    assert sql.lstrip().startswith("UPDATE tenant_event_outbox")
    assert "published_at IS NULL" in sql
    assert args == ("7", NOW)


async def test_record_failure_counts_the_attempt_and_never_sets_published_at() -> None:
    pool = RecordingPool()
    await _store(pool).record_failure("7", "KafkaTimeoutError: x")
    sql, args = pool.calls[0]
    assert "attempts = attempts + 1" in sql
    assert "last_error" in sql
    assert "published_at" not in sql.split("WHERE")[0]
    assert args == ("7", "KafkaTimeoutError: x")


async def test_a_driver_failure_names_the_class_only() -> None:
    pool = RecordingPool(fail=True)
    with pytest.raises(TenantOutboxStoreUnavailableError) as raised:
        await _store(pool).fetch_unpublished(1)
    assert str(raised.value) == "InterfaceError"
    assert "secret-detail" not in repr(raised.value)


async def test_no_pool_and_no_dsn_is_refused_without_connecting() -> None:
    with pytest.raises(TenantOutboxStoreUnavailableError):
        await TenantOutboxPostgres().fetch_unpublished(1)
