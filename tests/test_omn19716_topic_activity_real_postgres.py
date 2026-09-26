# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19716: real-Postgres write-path proof for the topic-activity projection.

The writer tests drive the same statements against a recording DB double, which
binds any Python type: an ISO string binds into a TIMESTAMPTZ as "successfully"
as a datetime, and a Decimal-or-float mix into DOUBLE PRECISION the same. Only a
real Postgres connection enforces column types through asyncpg (the OMN-15905
class). The stale-sample guard and disappearance-to-ABSENT update are implemented
in SQL and have no Python branch, so a real database is the only place they can
be proven. The harness mirrors tests/test_omn18768_runner_fleet_real_postgres_write_path.py:
it SKIPS without a reachable database and uses its own throwaway schema.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote_plus

import asyncpg
import pytest

from omnimarket.nodes.node_projection_topic_activity.handlers.handler_topic_activity_writer import (
    _MARK_DISAPPEARED_ABSENT,
    _SELECT_DISAPPEARED,
    _UPSERT_TOPIC,
)

_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_topic_activity"
    / "migrations"
    / "0000_create_topic_activity.sql"
)
_T0 = datetime(2026, 9, 26, 11, 57, 9, tzinfo=UTC)
_SCHEMA = "omn19716_topic_activity_write_path_test"
_TOPIC = "onex.evt.omniclaude.tool-executed.v1"


async def _connect_or_skip() -> asyncpg.Connection:
    password = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    if not password:
        pytest.skip(
            "INTEGRATION_POSTGRES_PASSWORD not set -- skipping topic-activity write-path DB proof"
        )
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = int(os.environ.get("INTEGRATION_POSTGRES_PORT", "5432"))
    user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
    db = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
    dsn = f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{db}"
    try:
        return await asyncpg.connect(dsn)
    except (
        OSError,
        asyncpg.PostgresError,
    ) as exc:  # pragma: no cover - infra-dependent
        pytest.skip(f"no reachable Postgres for topic-activity write-path proof: {exc}")


def _scoped(statement: str) -> str:
    return statement.replace("omninode_internal.", f"{_SCHEMA}.")


async def _setup(conn: asyncpg.Connection) -> None:
    await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
    await conn.execute(f"CREATE SCHEMA {_SCHEMA}")
    await conn.execute(
        _MIGRATION.read_text(encoding="utf-8").replace(
            "omninode_internal.", f"{_SCHEMA}."
        )
    )


async def _upsert(
    conn: asyncpg.Connection, *, topic: str, sampled_at: datetime, last_hour: int | None
) -> list[asyncpg.Record]:
    return await conn.fetch(
        _scoped(_UPSERT_TOPIC),
        topic,
        sampled_at,
        70_000,
        4_000,
        66_000,
        None if last_hour is None else 12,
        None if last_hour is None else 0.4,
        last_hour,
        64_917,
        None if last_hour is None else last_hour / 3600,
        False,
        sampled_at - timedelta(seconds=1),
        1.0,
        "ACTIVE" if last_hour else "UNKNOWN",
    )


@pytest.mark.integration
async def test_real_postgres_accepts_the_writers_bound_types_and_nulls() -> None:
    """A first sample's null rate lands as NULL, never 0, with typed timestamps."""
    conn = await _connect_or_skip()
    try:
        await _setup(conn)
        rows = await _upsert(conn, topic=_TOPIC, sampled_at=_T0, last_hour=None)
        assert len(rows) == 1
        row = rows[0]
        assert isinstance(row["sampled_at"], datetime)
        assert row["rate_per_second"] is None
        assert row["messages_last_hour"] is None
        assert isinstance(row["projection_cursor"], int)
    finally:
        await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        await conn.close()


@pytest.mark.integration
async def test_real_postgres_refuses_a_stale_sample() -> None:
    """An older sample returns no row, so the writer never republishes it."""
    conn = await _connect_or_skip()
    try:
        await _setup(conn)
        newer = _T0 + timedelta(seconds=30)
        assert await _upsert(conn, topic=_TOPIC, sampled_at=newer, last_hour=1_209)
        assert await _upsert(conn, topic=_TOPIC, sampled_at=_T0, last_hour=1) == []
        stored = await conn.fetchval(
            f"SELECT messages_last_hour FROM {_SCHEMA}.topic_activity WHERE topic = $1",
            _TOPIC,
        )
        assert stored == 1_209
        # Positive control: a newer sample is accepted.
        assert await _upsert(
            conn,
            topic=_TOPIC,
            sampled_at=newer + timedelta(seconds=30),
            last_hour=1_300,
        )
    finally:
        await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        await conn.close()


@pytest.mark.integration
async def test_real_postgres_marks_only_missing_topics_absent() -> None:
    conn = await _connect_or_skip()
    try:
        await _setup(conn)
        await _upsert(conn, topic=_TOPIC, sampled_at=_T0, last_hour=10)
        await _upsert(conn, topic="onex.evt.retired.v1", sampled_at=_T0, last_hour=0)
        later = _T0 + timedelta(seconds=30)
        candidates = [
            r["topic"]
            for r in await conn.fetch(_scoped(_SELECT_DISAPPEARED), later, [_TOPIC])
        ]
        assert candidates == ["onex.evt.retired.v1"]
        updated = await conn.fetch(
            _scoped(_MARK_DISAPPEARED_ABSENT),
            "onex.evt.retired.v1",
            later,
            "ABSENT",
        )
        assert [r["topic"] for r in updated] == ["onex.evt.retired.v1"]
        assert updated[0]["activity_state"] == "ABSENT"
        assert updated[0]["sampled_at"] == later
        assert updated[0]["retained_messages"] == 66_000
        stored = await conn.fetch(
            f"SELECT topic, sampled_at, activity_state "
            f"FROM {_SCHEMA}.topic_activity ORDER BY topic"
        )
        assert [r["topic"] for r in stored] == [
            "onex.evt.retired.v1",
            _TOPIC,
        ]
        assert stored[0]["activity_state"] == "ABSENT"
        assert stored[1]["activity_state"] == "ACTIVE"
        assert stored[1]["sampled_at"] == _T0
    finally:
        await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        await conn.close()
