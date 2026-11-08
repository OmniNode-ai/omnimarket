# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A runtime projection writer keeps its pools for its process, not one delivery.

OMN-23196. Before, ``RuntimeProjectionWriter.handle`` opened and attested one
pool per topology binding for every delivery and closed them again, so each
offset commit waited on that setup. These tests run the real writer, runner,
topology resolution and ``AsyncpgAdapter``; only the asyncpg driver is a fake
that counts ``create_pool`` calls and records each transaction's end.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import asyncpg
import pytest
from omnibase_infra.runtime.auto_wiring import (
    handler_wiring,
    standalone_projection_bindings,
)

from omnimarket.nodes.node_projection_savings.handlers.handler_runtime_writer import (
    SavingsProjectionWriter,
)
from omnimarket.projection.runner import BaseProjectionRunner

pytestmark = pytest.mark.unit

TENANT = "35ef29ac-3b39-423f-9675-32597b14f0d3"
TOPIC = "onex.evt.omnibase-infra.savings-estimated.v1"
DELIVERIES = 100


class _FakeConnection:
    def __init__(self, pool: _FakePool) -> None:
        self.pool = pool

    def transaction(self) -> _FakeTransaction:
        return _FakeTransaction(self.pool)

    async def execute(self, *_args: object) -> str:
        return "OK"

    async def fetchval(self, sql: str, *args: object) -> str | None:
        if sql == "SELECT current_user":
            return self.pool.dsn.rsplit("/", 1)[-1]
        if "WHERE tenant_uuid" in sql:
            return str(args[0])
        return None

    async def fetch(self, sql: str, *args: object) -> list[dict[str, Any]]:
        self.pool.loops.add(asyncio.get_running_loop())
        if self.pool.fail_next:
            self.pool.fail_next = False
            raise asyncpg.exceptions.DataError("fixture bad payload")
        self.pool.statements.append(" ".join(sql.split()))
        if sql.lstrip().startswith("SELECT"):
            return []
        return [{"tenant_id": TENANT, "event_id": "fixture-event"}]


class _FakeTransaction:
    def __init__(self, pool: _FakePool) -> None:
        self.pool = pool

    async def __aenter__(self) -> None:
        self.pool.transactions.append("begin")

    async def __aexit__(self, exc_type: object, *_rest: object) -> None:
        self.pool.transactions.append("rollback" if exc_type else "commit")


class _Acquire:
    def __init__(self, pool: _FakePool) -> None:
        self.pool = pool

    async def __aenter__(self) -> _FakeConnection:
        assert not self.pool.closed, "a delivery used a closed pool"
        return _FakeConnection(self.pool)

    async def __aexit__(self, *_exc: object) -> None:
        return None


class _FakePool:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self.closed = False
        self.fail_next = False
        self.statements: list[str] = []
        self.transactions: list[str] = []
        self.loops: set[asyncio.AbstractEventLoop] = set()

    def acquire(self) -> _Acquire:
        return _Acquire(self)

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def pools(monkeypatch: pytest.MonkeyPatch) -> list[_FakePool]:
    """Fake the driver only; topology resolution and writer SQL stay real."""
    created: list[_FakePool] = []

    async def create_pool(dsn: str, **_kwargs: object) -> _FakePool:
        pool = _FakePool(dsn)
        created.append(pool)
        return pool

    def dsn(binding: object, resolver: object = None) -> str:
        return f"postgresql://fixture.invalid/{binding.principal}"  # type: ignore[attr-defined]

    monkeypatch.setenv("ONEX_DATABASE_TOPOLOGY_PROFILE", "local")
    monkeypatch.setattr(handler_wiring, "_resolve_binding_dsn", dsn)
    monkeypatch.setattr(standalone_projection_bindings, "_resolve_binding_dsn", dsn)
    monkeypatch.setattr(
        handler_wiring, "build_topology_secret_resolver", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(asyncpg, "create_pool", create_pool)
    monkeypatch.setattr(
        BaseProjectionRunner, "publish_snapshot_delta", AsyncMock(return_value=True)
    )
    return created


@pytest.fixture
def writer() -> Iterator[SavingsProjectionWriter]:
    instance = SavingsProjectionWriter()
    yield instance
    instance.close()


def _request(offset: int) -> dict[str, Any]:
    return {
        "_topic": TOPIC,
        "_partition": 2,
        "_offset": offset,
        "tenant_id": TENANT,
        "event_timestamp": "2026-10-27T00:00:00+00:00",
        "session_id": f"fixture-session-{offset}",
        "model_local": "fixture-local",
        "model_cloud_baseline": "fixture-cloud",
        "local_cost_usd": "1",
        "cloud_cost_usd": "2",
        "savings_usd": "1",
    }


def test_a_hundred_deliveries_create_each_pool_once(
    pools: list[_FakePool], writer: SavingsProjectionWriter
) -> None:
    assert writer.handle(_request(0)) == {"projected": True}
    per_process = len(pools)
    # One pool per topology binding the contract declares; the writer used to
    # open this many for every delivery.
    assert per_process >= 2

    for offset in range(1, DELIVERIES):
        assert writer.handle(_request(offset)) == {"projected": True}

    assert len(pools) == per_process, "a delivery opened a pool"
    assert not any(pool.closed for pool in pools), "a delivery closed a pool"
    inserts = [s for pool in pools for s in pool.statements if "INSERT" in s]
    assert len(inserts) >= DELIVERIES


def test_each_delivery_commits_in_its_own_transaction(
    pools: list[_FakePool], writer: SavingsProjectionWriter
) -> None:
    for offset in range(5):
        writer.handle(_request(offset))
    log = [t for pool in pools for t in pool.transactions]
    assert log.count("begin") == log.count("commit") > 0
    assert "rollback" not in log
    # Each begin is closed before the next opens, so no delivery shares one.
    for pool in pools:
        for begin, end in zip(
            pool.transactions[::2], pool.transactions[1::2], strict=True
        ):
            assert (begin, end) == ("begin", "commit")


def test_a_failing_delivery_rolls_back_alone_and_the_next_one_commits(
    pools: list[_FakePool], writer: SavingsProjectionWriter
) -> None:
    writer.handle(_request(0))
    count = len(pools)
    writing = next(
        pool for pool in pools if any("INSERT" in s for s in pool.statements)
    )
    committed_before = writing.transactions.count("commit")
    writing.fail_next = True

    with pytest.raises(asyncpg.exceptions.DataError, match="fixture bad payload"):
        writer.handle(_request(1))

    assert writing.transactions.count("rollback") == 1
    assert writing.transactions.count("commit") == committed_before
    assert len(pools) == count
    assert not any(pool.closed for pool in pools)

    assert writer.handle(_request(2)) == {"projected": True}
    assert writing.transactions.count("rollback") == 1
    assert writing.transactions.count("commit") > committed_before
    assert len(pools) == count


def test_a_connection_failure_reopens_the_pools_for_the_next_delivery(
    pools: list[_FakePool], writer: SavingsProjectionWriter
) -> None:
    writer.handle(_request(0))
    first = list(pools)

    victim = next(pool for pool in pools if any("INSERT" in s for s in pool.statements))

    async def refuse(*_args: object) -> list[dict[str, Any]]:
        raise ConnectionResetError("fixture connection lost")

    original = _FakeConnection.fetch
    _FakeConnection.fetch = lambda self, *a: (  # type: ignore[method-assign]
        refuse() if self.pool is victim else original(self, *a)
    )
    try:
        with pytest.raises(ConnectionResetError):
            writer.handle(_request(1))
    finally:
        _FakeConnection.fetch = original  # type: ignore[method-assign]

    assert all(pool.closed for pool in first)
    assert writer.handle(_request(2)) == {"projected": True}
    assert len(pools) == 2 * len(first)
    assert not any(pool.closed for pool in pools[len(first) :])


def test_close_closes_every_pool_once_and_the_writer_can_start_again(
    pools: list[_FakePool], writer: SavingsProjectionWriter
) -> None:
    writer.handle(_request(0))
    opened = list(pools)
    thread = writer._thread
    assert thread is not None
    assert thread.is_alive()

    writer.close()
    writer.close()

    assert all(pool.closed for pool in opened)
    assert not thread.is_alive()
    assert writer.handle(_request(1)) == {"projected": True}
    assert len(pools) == 2 * len(opened)


def test_every_delivery_runs_on_the_one_loop_even_from_concurrent_threads(
    pools: list[_FakePool], writer: SavingsProjectionWriter
) -> None:
    errors: list[BaseException] = []

    def deliver(offsets: range) -> None:
        try:
            for offset in offsets:
                writer.handle(_request(offset))
        except BaseException as err:
            errors.append(err)

    threads = [
        threading.Thread(target=deliver, args=(range(i * 10, i * 10 + 10),))
        for i in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    assert len({loop for pool in pools for loop in pool.loops}) == 1
    assert len(pools) == len({pool.dsn for pool in pools})
