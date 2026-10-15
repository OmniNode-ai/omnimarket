# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Lab races at the real write/publication boundaries, before implementation.

Failures: a sparse writer computes before another writer records usage; the
last writer stores or publishes that stale figure; an async-only lock protects
neither a sync competitor nor another SQLite connection. Every schedule uses
fresh run ids and real storage. Only the scheduling boundary and snapshot
transport are intercepted. PostgreSQL is owned by the lab harness; SQLite is
an actual temporary store on that same lab host. Neither is a laptop proof.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    HandlerProjectionDelegation,
)
from omnimarket.projection.postgres_sync_database import PostgresSyncProjectionAdapter
from omnimarket.projection.runner import MessageMeta
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter
from tests.projection.test_omn21015_run_savings_served import TENANT, store
from tests.projection.test_omn21015_run_savings_served import priced as priced
from tests.test_omn15909_real_postgres_projection_write_path_gate import (
    _provisioned_runner,
)

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("priced")]


def _rows() -> tuple[dict[str, object], dict[str, object]]:
    common: dict[str, object] = {
        "correlation_id": str(uuid4()),
        "tenant_id": TENANT,
        "created_at": datetime(2026, 10, 13, 12, tzinfo=UTC),
        "timestamp": datetime(2026, 10, 13, 12, tzinfo=UTC),
        "task_type": "document",
        "delegated_to": "fixture-serving-model",
    }
    return dict(common), {
        **common,
        "tokens_input": 1000,
        "tokens_output": 200,
        "cost_usd": Decimal("0.004"),
    }


def _paired(row: dict[str, Any]) -> None:
    assert (row["tokens_input"], row["tokens_output"]) == (1000, 200)
    assert Decimal(str(row["cost_usd"])) == Decimal("0.004")
    assert row["modelled_savings_usd"] is not None, row
    assert Decimal(str(row["modelled_savings_usd"])) == Decimal("0.010")
    assert row["savings_measurement"] == "measured"
    assert row["savings_baseline_model"] == "fixture-baseline"
    assert row["savings_pricing_manifest_version"] == "fixture-v1"


def _handler(
    monkeypatch: pytest.MonkeyPatch, published: list[dict[str, Any]]
) -> HandlerProjectionDelegation:
    handler = HandlerProjectionDelegation()
    monkeypatch.setattr(
        handler, "_publish_row_snapshot", lambda rows: published.extend(rows)
    )
    monkeypatch.setattr(handler, "_publish_aggregate_snapshots", lambda *_: None)
    return handler


def _hold_sync_write(
    monkeypatch: pytest.MonkeyPatch, adapter: Any
) -> tuple[threading.Event, threading.Event]:
    entered, release = threading.Event(), threading.Event()
    # The legacy implementation prices before upsert_returning. The new
    # capability must price inside upsert_derived_returning. Holding either
    # actual entry point lets precisely the same test exercise both designs.
    for name in ("upsert_returning", "upsert_derived_returning"):
        if not hasattr(adapter, name):
            continue
        operation = getattr(adapter, name)

        def held(*args: Any, _operation: Any = operation, **kwargs: Any) -> Any:
            entered.set()
            if not release.wait(15):
                raise TimeoutError("the test did not release its owned writer")
            return _operation(*args, **kwargs)

        monkeypatch.setattr(adapter, name, held)
    return entered, release


@pytest.mark.parametrize("held_path", ["async", "sync"])
@pytest.mark.parametrize("held_row", ["sparse", "terminal"])
async def test_postgres_mixed_writers_return_and_publish_the_committed_pair(
    integration_postgres_dsn: str,
    held_path: str,
    held_row: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _provisioned_runner(integration_postgres_dsn) as (runner, conn, schema):
        adapter = PostgresSyncProjectionAdapter(integration_postgres_dsn, schema=schema)
        sync_published: list[dict[str, Any]] = []
        async_published: list[dict[str, Any]] = []
        handler = _handler(monkeypatch, sync_published)

        async def published(rows: list[dict[str, Any]], _: MessageMeta) -> None:
            async_published.extend(rows)

        monkeypatch.setattr(runner, "_publish_row_snapshot", published)
        original = runner._dynamic_upsert
        for repeat in range(3):
            sparse, terminal = _rows()
            first_row, second_row = (
                (sparse, terminal) if held_row == "sparse" else (terminal, sparse)
            )
            entered, release = threading.Event(), threading.Event()
            if held_path == "async":

                async def held_upsert(
                    _entered: threading.Event = entered,
                    _release: threading.Event = release,
                    **kwargs: Any,
                ) -> list[dict[str, Any]]:
                    _entered.set()
                    if not await asyncio.to_thread(_release.wait, 15):
                        raise TimeoutError("the test did not release its owned writer")
                    return await original(**kwargs)

                monkeypatch.setattr(runner, "_dynamic_upsert", held_upsert)
            else:
                entered, release = _hold_sync_write(monkeypatch, adapter)
            meta = MessageMeta(
                partition=0, offset=repeat, fallback_id=str(sparse["correlation_id"])
            )
            sync_published.clear()
            async_published.clear()
            first = asyncio.create_task(
                runner._write_delegation_row(first_row, meta)
                if held_path == "async"
                else asyncio.to_thread(
                    handler._write_delegation_row, adapter, first_row
                )
            )
            assert await asyncio.to_thread(entered.wait, 15), (
                "writer never reached its storage boundary"
            )
            try:
                if held_path == "async":
                    await asyncio.wait_for(
                        asyncio.to_thread(
                            handler._write_delegation_row, adapter, second_row
                        ),
                        15,
                    )
                else:
                    await asyncio.wait_for(
                        runner._write_delegation_row(second_row, meta), 15
                    )
            finally:
                release.set()
                await first
            stored = await conn.fetchrow(
                "SELECT * FROM delegation_events WHERE correlation_id=$1",
                sparse["correlation_id"],
            )
            assert stored is not None
            _paired(dict(stored))
            final_publication = (
                async_published[-1] if held_path == "async" else sync_published[-1]
            )
            _paired(final_publication)
            assert final_publication == {key: stored[key] for key in final_publication}


@pytest.mark.parametrize("held_row", ["sparse", "terminal"])
async def test_two_sqlite_connections_return_and_publish_the_committed_pair(
    tmp_path: Path, held_row: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_adapter = store(tmp_path)
    second_adapter = SqliteDatabaseAdapter(tmp_path / "runs.sqlite")
    first_published: list[dict[str, Any]] = []
    second_published: list[dict[str, Any]] = []
    first_handler = _handler(monkeypatch, first_published)
    second_handler = _handler(monkeypatch, second_published)
    entered, release = _hold_sync_write(monkeypatch, first_adapter)
    for _ in range(3):
        entered.clear()
        release.clear()
        sparse, terminal = _rows()
        first_row, second_row = (
            (sparse, terminal) if held_row == "sparse" else (terminal, sparse)
        )
        first_published.clear()
        first = asyncio.create_task(
            asyncio.to_thread(
                first_handler._write_delegation_row, first_adapter, first_row
            )
        )
        assert await asyncio.to_thread(entered.wait, 15), (
            "writer never reached its storage boundary"
        )
        try:
            await asyncio.wait_for(
                asyncio.to_thread(
                    second_handler._write_delegation_row, second_adapter, second_row
                ),
                15,
            )
        finally:
            release.set()
            await first
        stored = second_adapter.query(
            "delegation_events", {"correlation_id": sparse["correlation_id"]}
        )
        assert len(stored) == 1
        _paired(stored[0])
        _paired(first_published[-1])
        assert first_published[-1] == {
            key: stored[0][key] for key in first_published[-1]
        }
