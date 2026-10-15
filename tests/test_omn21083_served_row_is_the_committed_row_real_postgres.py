# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A mutable delegation row is served as committed, in any snapshot arrival order.

OMN-21083. Two writers of one run each commit to ``delegation_events`` and then
publish the row their statement returned. When the first writer's publish is
delayed past the second writer's, the two snapshots arrive in the opposite
order to their commits, from two source topics whose offsets cannot be
compared. ``SnapshotCache`` keeps whichever arrives last, so a client served
from it would read the OLDER row.

The projection API answers every route from the writer's table instead
(OMN-20152). This module drives that race through the real writers against real
Postgres and reads ``delegation.decisions.v1`` through the real
``/projection/{topic}`` route. Failure modes it is written against:

- F1 the route serves the first-committed row (0.004) rather than the stored
  one (0.009) -- what a read served from the snapshot cache returns after this
  race, and what a return to cache serving would bring back;
- F2 the race does not happen (the snapshots arrive in commit order), so a
  correct read proves nothing about ordering -- the arrival order is asserted.

The database is the INTEGRATION_POSTGRES_* one; the module skips without one.
"""

from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest

from omnimarket.projection.api_server import app, get_row_source, get_topic_map
from omnimarket.projection.discovery import build_projection_topic_map
from omnimarket.projection.runner import MessageMeta
from omnimarket.projection.table_reader import TableRowSource
from tests.test_omn15909_real_postgres_projection_write_path_gate import (
    _base_dsn,
    _provisioned_runner,
)

_DECISIONS = "onex.snapshot.projection.delegation.decisions.v1"
_TENANT = "00000000-0000-4000-8000-000000021083"
_FIRST_COST = Decimal("0.004")
_SECOND_COST = Decimal("0.009")


@pytest.fixture
def _no_overrides() -> Iterator[None]:
    yield
    app.dependency_overrides.clear()


@pytest.mark.integration
@pytest.mark.usefixtures("_no_overrides")
async def test_reversed_snapshots_still_serve_the_row_committed_last(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _provisioned_runner() as (first, admin_conn, schema):
        second = copy.copy(first)
        arrivals: list[tuple[str, Any]] = []

        class _RecordingProducer:
            async def send_and_wait(
                self, topic: str, *, value: bytes, key: bytes, headers: Any
            ) -> None:
                if topic == _DECISIONS:
                    arrivals.append((topic, value))

        async def _producer() -> _RecordingProducer:
            return _RecordingProducer()

        monkeypatch.setattr(first, "_ensure_producer", _producer)
        monkeypatch.setattr(second, "_ensure_producer", _producer)

        # Hold the first writer's publish until the second writer has
        # committed and published: the commits run first -> second, the
        # snapshots arrive second -> first.
        ready, release = asyncio.Event(), asyncio.Event()
        publish = first._publish_row_snapshot

        async def _held_publish(written: Any, meta: MessageMeta) -> None:
            ready.set()
            await release.wait()
            await publish(written, meta)

        monkeypatch.setattr(first, "_publish_row_snapshot", _held_publish)

        correlation_id = f"omn21083-{schema}"
        base = {
            "correlation_id": correlation_id,
            "tenant_id": _TENANT,
            "created_at": datetime(2026, 10, 15, tzinfo=UTC),
            "task_type": "document",
            "delegated_to": "fixture",
            "cost_usd": _FIRST_COST,
        }
        first_write = asyncio.create_task(
            first._write_delegation_row(
                dict(base),
                MessageMeta(
                    topic="source-a", partition=0, offset=1, fallback_id=correlation_id
                ),
            )
        )
        await asyncio.wait_for(ready.wait(), 10)
        try:
            await second._write_delegation_row(
                {**base, "cost_usd": _SECOND_COST},
                MessageMeta(
                    topic="source-b", partition=0, offset=1, fallback_id=correlation_id
                ),
            )
        finally:
            release.set()
            await first_write

        stored = await admin_conn.fetchval(
            "SELECT cost_usd FROM delegation_events WHERE correlation_id = $1",
            correlation_id,
        )
        assert stored == _SECOND_COST

        # F2: the snapshots really did arrive newest first.
        arrived_costs = [
            Decimal(str(json.loads(value)["row"]["cost_usd"])) for _, value in arrivals
        ]
        assert arrived_costs == [_SECOND_COST, _FIRST_COST]

        # F1: the route a client calls serves the stored row.
        source = TableRowSource.for_database_url(_base_dsn())
        try:
            topics = {
                topic: cfg.model_copy(update={"relation_schema": schema})
                for topic, cfg in build_projection_topic_map().items()
                if topic == _DECISIONS
            }
            app.dependency_overrides[get_topic_map] = lambda: topics
            app.dependency_overrides[get_row_source] = lambda: source
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost"
            ) as client:
                response = await client.get(
                    f"/projection/{_DECISIONS}",
                    params={"tenant": _TENANT, "correlation_id": correlation_id},
                )
        finally:
            await source.close()
        assert response.status_code == 200, response.text
        rows = response.json()["rows"]
        assert len(rows) == 1, rows
        assert Decimal(str(rows[0]["cost_usd"])) == _SECOND_COST
