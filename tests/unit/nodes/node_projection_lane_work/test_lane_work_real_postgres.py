# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26708: real-Postgres write-path gate for the lane work projection.

The database double in ``test_lane_work_writer.py`` evaluates the seq guard in
Python. Only a real Postgres evaluates the upsert's own SQL: the ``WHERE seq <
EXCLUDED.seq`` guard, the counters added in the update arm, the ``duration``
computed from the stored ``started_at``, ``RETURNING`` on a refused row, and the
terminal-rows view. A throwaway schema carries the node's real migration, so
concurrent runs never collide. Without a reachable database the tests skip.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote_plus
from uuid import uuid4

import asyncpg
import pytest

from omnimarket.nodes.node_projection_lane_work.contract_topics import (
    TOPIC_LANE_WORK_TRANSITION,
)
from omnimarket.nodes.node_projection_lane_work.handlers import (
    handler_projection_lane_work as module,
)
from omnimarket.nodes.node_projection_lane_work.handlers.handler_projection_lane_work import (
    LaneWorkProjectionWriter,
)
from omnimarket.projection.runner import MessageMeta
from tests.unit.nodes.node_projection_lane_work.receipts import (
    blocked_workflow,
    handed_off_workflow,
    live_workflow,
)

MIGRATION = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_projection_lane_work/migrations"
    / "0000_create_lane_work_workflow.sql"
)


def _dsn() -> str:
    password = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = os.environ.get("INTEGRATION_POSTGRES_PORT", "5432")
    user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
    db = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
    return f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{db}"


async def _connect_or_skip() -> asyncpg.Connection:
    if not os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    ):
        pytest.skip("no Postgres password set for the OMN-26708 write-path gate")
    try:
        return await asyncpg.connect(_dsn())
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"no reachable Postgres for the OMN-26708 write-path gate: {exc}")
        raise AssertionError("unreachable") from exc


class _ConnectionDb:
    """The two methods the writer calls, bound to one disposable connection."""

    def __init__(self, connection: asyncpg.Connection) -> None:
        self._connection = connection

    async def fetchval(self, sql: str, *args: Any) -> Any:
        return await self._connection.fetchval(sql, *args)

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None


@asynccontextmanager
async def _migrated_writer() -> AsyncIterator[
    tuple[LaneWorkProjectionWriter, asyncpg.Connection, str]
]:
    connection = await _connect_or_skip()
    schema = f"omn26708_{uuid4().hex[:12]}"
    originals = {name: getattr(module, name) for name in ("_UPSERT", "_SELECT_ROW")}
    try:
        await connection.execute(f"CREATE SCHEMA {schema}")
        await connection.execute(
            MIGRATION.read_text().replace("omninode_internal.", f"{schema}.")
        )
        writer = LaneWorkProjectionWriter()
        cast(Any, writer)._db = _ConnectionDb(connection)

        async def _capture(exposure: Any, **kwargs: Any) -> bool:
            return True

        cast(Any, writer).publish_snapshot_delta = _capture
        for name, sql in originals.items():
            setattr(module, name, sql.replace("omninode_internal.", f"{schema}."))
        yield writer, connection, schema
    finally:
        for name, sql in originals.items():
            setattr(module, name, sql)
        await connection.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await connection.close()


async def _deliver_all(
    writer: LaneWorkProjectionWriter, receipts: list[Any]
) -> list[int]:
    counts = []
    for offset, receipt in enumerate(receipts):
        meta = MessageMeta(
            partition=0,
            offset=offset,
            fallback_id=f"o{offset}",
            topic=TOPIC_LANE_WORK_TRANSITION,
        )
        written = await writer._project_and_report(
            TOPIC_LANE_WORK_TRANSITION, receipt.model_dump(mode="json"), meta
        )
        counts.append(len(written))
    return counts


@pytest.mark.integration
@pytest.mark.asyncio
async def test_n_workflows_give_n_rows_and_a_replay_changes_nothing() -> None:
    receipts = [
        *live_workflow("a-1-1"),
        *handed_off_workflow("b-2-1"),
        *blocked_workflow("c-3-1"),
    ]
    async with _migrated_writer() as (writer, connection, schema):
        first = await _deliver_all(writer, receipts)
        assert first == [1] * len(receipts)
        query = f"SELECT * FROM {schema}.lane_work_workflow ORDER BY workflow_id"
        table = [dict(r) for r in await connection.fetch(query)]
        assert [r["workflow_id"] for r in table] == ["a-1-1", "b-2-1", "c-3-1"]

        replayed = await _deliver_all(writer, receipts)
        assert replayed == [0] * len(receipts)
        assert [dict(r) for r in await connection.fetch(query)] == table

        ended = {r["workflow_id"]: r for r in table}["b-2-1"]
        assert ended["duration_seconds"] == 25 * 60
        assert ended["help_request_count"] == 1
        assert ended["transitions_taken"] == 8
        assert ended["terminal_kind"] == "handed_off"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_terminal_rows_hold_only_ended_workflows() -> None:
    receipts = [
        *live_workflow("a-1-1"),
        *handed_off_workflow("b-2-1"),
        *blocked_workflow("c-3-1"),
    ]
    async with _migrated_writer() as (writer, connection, schema):
        await _deliver_all(writer, receipts)
        rows = await connection.fetch(
            f"SELECT workflow_id FROM {schema}.lane_work_workflow "
            "WHERE terminal_kind IS NOT NULL ORDER BY workflow_id"
        )
        assert [r["workflow_id"] for r in rows] == ["b-2-1", "c-3-1"]
