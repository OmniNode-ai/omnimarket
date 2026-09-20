# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18769: real-Postgres write-path gate for the lab lane-health projection.

Why this is not redundant with the golden chain or the unit suite: both drive
the same ``project_event()`` against a database double, and a double accepts a
bound parameter of any Python type -- an ISO ``str`` binds as "successfully" as
a real ``datetime`` into a column declared ``TIMESTAMPTZ``. Only a real
Postgres connection enforces column types through asyncpg's extended query
protocol. That gap is exactly how the OMN-15905 str-where-datetime defect
reached a deployed, crash-looping runtime with every layer of mock coverage
green.

Real Postgres, never SQLite: SQLite's loosely-affinity-typed columns accept a
``str`` into a TIMESTAMP column without complaint, which is the hole this class
of gate exists to close.

What this file proves that nothing else can:

1. the migration's DDL is valid and the writer's SQL binds against it;
2. the three per-fact upserts each touch ONLY their own column group, so three
   concurrent consume loops do not clobber each other;
3. the ``ON CONFLICT ... WHERE observed_at <= EXCLUDED`` guard actually refuses
   an out-of-order redelivery IN SQL, which a check-then-act in Python could
   not do safely and a mock cannot evaluate at all;
4. ``row_to_json`` round-trips every column back into the typed row the
   republish path renders.

Harness pattern mirrors ``tests/test_omn15909_real_postgres_projection_write_path_gate.py``:
it SKIPS (never ERRORs) without a reachable database and provisions a throwaway
schema so concurrent runs never collide.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus
from uuid import uuid4

import asyncpg
import pytest

from omnimarket.nodes.node_projection_lab_lane_health.contract_topics import (
    TOPIC_LAB_PASS_RECEIPT,
    TOPIC_LANE_CENSUS,
    TOPIC_RUNTIME_HEALTH,
)
from omnimarket.nodes.node_projection_lab_lane_health.handlers import (
    handler_lab_lane_health_runner as handler_lab_lane_health_runner_module,
)
from omnimarket.nodes.node_projection_lab_lane_health.handlers.handler_lab_lane_health_runner import (
    LabLaneHealthProjectionWriter,
    row_from_record,
)
from omnimarket.nodes.node_projection_lab_lane_health.models.enum_fact_status import (
    EnumFactStatus,
)

# Both forms deliberately: the module mark is what pytest selects on, and the
# per-test decorator below is what scripts/ci/check_projection_write_path_db_gate.py
# reads to confirm a write-path change brought a real-Postgres test with it.
pytestmark = pytest.mark.integration

MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_projection_lab_lane_health/migrations"
    / "0000_create_lab_lane_health.sql"
)
NOW = datetime(2026, 9, 18, 23, 0, tzinfo=UTC)
LAB_HOST = "lab-host"


def _base_dsn() -> str:
    password = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = os.environ.get("INTEGRATION_POSTGRES_PORT", "5432")
    user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
    db = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
    return f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{db}"


async def _connect_or_skip() -> asyncpg.Connection:
    password = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    if not password:
        pytest.skip(
            "POSTGRES_PASSWORD not set -- skipping the OMN-18769 real-Postgres "
            "lab lane-health write-path gate"
        )
        raise AssertionError("unreachable: pytest.skip always raises")
    try:
        return await asyncpg.connect(_base_dsn())
    except (OSError, asyncpg.PostgresError) as exc:  # pragma: no cover - infra
        pytest.skip(f"no reachable Postgres for the OMN-18769 write-path gate: {exc}")
        raise AssertionError("unreachable: pytest.skip always raises") from exc


class _ConnectionDb:
    """Adapter shim exposing the two methods the writer calls on one connection.

    The writer only ever calls ``execute`` and ``fetchval``; binding those to a
    single disposable connection keeps the test's schema isolation intact,
    which a pooled adapter would not.
    """

    def __init__(self, connection: asyncpg.Connection) -> None:
        self._connection = connection

    async def execute(self, sql: str, *args: Any) -> None:
        await self._connection.execute(sql, *args)

    async def fetchval(self, sql: str, *args: Any) -> Any:
        return await self._connection.fetchval(sql, *args)


class _MessageMeta:
    topic = TOPIC_LANE_CENSUS
    partition = 0
    offset = 1
    fallback_id = "omn18769"


@asynccontextmanager
async def _migrated_handler() -> AsyncIterator[
    tuple[LabLaneHealthProjectionWriter, asyncpg.Connection, str]
]:
    """A throwaway schema carrying the real migration, wired to the real writer."""
    connection = await _connect_or_skip()
    schema = f"omn18769_{uuid4().hex[:12]}"
    published: list[dict[str, Any]] = []
    try:
        await connection.execute(f"CREATE SCHEMA {schema}")
        ddl = MIGRATION.read_text().replace("omninode_internal.", f"{schema}.")
        await connection.execute(ddl)

        handler = LabLaneHealthProjectionWriter()
        handler._db = _ConnectionDb(connection)  # type: ignore[assignment]

        async def _capture(exposure: Any, **kwargs: Any) -> bool:
            published.append(dict(kwargs))
            return True

        handler.publish_snapshot_delta = _capture  # type: ignore[assignment]
        # Point the writer's SQL at the disposable schema. The module-level
        # statements are formatted from one TABLE constant, so rebinding them
        # here rewrites every one consistently rather than per call site.
        module = handler_lab_lane_health_runner_module

        originals = {
            name: getattr(module, name)
            for name in (
                "_UPSERT_CENSUS",
                "_UPSERT_HEALTH",
                "_UPSERT_RECEIPT",
                "_SELECT_ROW",
            )
        }
        for name, sql in originals.items():
            setattr(module, name, sql.replace("omninode_internal.", f"{schema}."))
        try:
            yield handler, connection, schema
        finally:
            for name, sql in originals.items():
                setattr(module, name, sql)
    finally:
        await connection.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await connection.close()


def _census(*, drift: int, at: datetime) -> dict[str, Any]:
    return {
        "host": LAB_HOST,
        "observed_at": at.isoformat(),
        "lanes_checked": ["dev"],
        "findings": [
            {
                "lane": "dev",
                "kind": "unexpected_container",
                "container": f"stray-{i}",
                "detail": "running but not declared",
                "severity": "warning",
            }
            for i in range(drift)
        ],
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_migration_ddl_is_valid_and_the_writer_binds_against_it() -> None:
    """The column-type question a mock cannot answer.

    ``observed_at`` is a real ``datetime`` all the way down; if any write path
    ever bound an ISO string instead, asyncpg refuses it here and nowhere else.
    """
    async with _migrated_handler() as (handler, connection, schema):
        await handler.project_event(
            TOPIC_LANE_CENSUS, _census(drift=2, at=NOW), _MessageMeta()
        )

        row = await connection.fetchrow(f"SELECT * FROM {schema}.lab_lane_health")
        assert row is not None
        assert row["lane"] == "compose-dev"
        assert row["census_drift_count"] == 2
        assert isinstance(row["census_observed_at"], datetime)
        assert isinstance(row["projected_at"], datetime)
        assert row["census_original_status"] == EnumFactStatus.FAIL.value


@pytest.mark.integration
@pytest.mark.asyncio
async def test_each_fact_touches_only_its_own_column_group() -> None:
    """Three producers at three cadences must not clobber each other.

    A whole-row read-modify-write would race the three consume loops; this
    asserts the per-fact upserts leave the other groups untouched.
    """
    async with _migrated_handler() as (handler, connection, schema):
        await handler.project_event(
            TOPIC_LANE_CENSUS, _census(drift=1, at=NOW), _MessageMeta()
        )
        await handler.project_event(
            TOPIC_LAB_PASS_RECEIPT,
            {
                "lane": "compose-dev",
                "sha": "a" * 40,
                "result": "FAIL",
                "finished_at": NOW.isoformat(),
                "checks": [
                    {"name": "ready_effects", "ok": False, "evidence": "HTTP_503"}
                ],
            },
            _MessageMeta(),
        )
        await handler.project_event(
            TOPIC_RUNTIME_HEALTH,
            {
                "lane": "compose-dev",
                "timestamp": NOW.isoformat(),
                "status": "HEALTHY",
                "dimensions": [{"name": "contract_discovery", "status": "HEALTHY"}],
            },
            _MessageMeta(),
        )

        row = await connection.fetchrow(f"SELECT * FROM {schema}.lab_lane_health")
        assert row is not None
        assert row["census_drift_count"] == 1
        assert row["receipt_result"] == "FAIL"
        assert json.loads(row["receipt_failing_checks"]) == ["ready_effects"]
        assert row["health_aggregate"] == "HEALTHY"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_out_of_order_redelivery_is_refused_in_sql() -> None:
    """The guard is a WHERE clause, not a check-then-act.

    Feed a NEWER census, then an OLDER one. The older must not win. A Python
    read-compare-write would race under concurrent consumers and let it.
    """
    async with _migrated_handler() as (handler, connection, schema):
        await handler.project_event(
            TOPIC_LANE_CENSUS, _census(drift=0, at=NOW), _MessageMeta()
        )
        await handler.project_event(
            TOPIC_LANE_CENSUS,
            _census(drift=9, at=NOW - timedelta(hours=6)),
            _MessageMeta(),
        )

        row = await connection.fetchrow(
            f"SELECT census_drift_count, census_observed_at FROM {schema}.lab_lane_health"
        )
        assert row is not None
        assert row["census_drift_count"] == 0, (
            "the older redelivery overwrote a newer fact"
        )
        assert row["census_observed_at"] == NOW


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_row_read_back_renders_every_dimension_it_stored() -> None:
    """The republish path reads the row back; the decode must be lossless.

    A dropped dimension here would blank the other two on every consumer keyed
    on ``lane`` -- data loss that reads as a producer bug.
    """
    async with _migrated_handler() as (handler, connection, schema):
        await handler.project_event(
            TOPIC_LANE_CENSUS,
            _census(drift=3, at=NOW - timedelta(hours=30)),
            _MessageMeta(),
        )
        await handler.project_event(
            TOPIC_RUNTIME_HEALTH,
            {
                "lane": "compose-dev",
                "timestamp": NOW.isoformat(),
                "status": "DEGRADED",
                "dimensions": [{"name": "consumer_groups", "status": "DEGRADED"}],
            },
            _MessageMeta(),
        )

        document = await connection.fetchval(
            f"SELECT row_to_json(t) FROM (SELECT * FROM {schema}.lab_lane_health) t"
        )
        wire = row_from_record(json.loads(document)).to_exposure_row(now=NOW)

        assert wire["census_drift_count"] == 3
        assert len(wire["census_drift_items"]) == 3
        assert wire["health_dimensions"][0]["name"] == "consumer_groups"
        # Each fact aged on its own clock, proven against stored values rather
        # than in-memory ones: the census fact is 30 hours old and the health
        # fact is current, and they resolve to different verdicts off the same
        # `now`.
        #
        # The census reads FAIL rather than STALE even at 30 hours, which is
        # past STALE_AFTER. A drift of 3 makes its ORIGINAL verdict FAIL, and
        # `decay` returns a FAIL unchanged at any age -- deliberately, because
        # decaying it would let a drifted lane read as merely old. That rule is
        # stated on the enum and pinned by the unit suite; this assertion used
        # to read STALE, which contradicted both and only surfaced once the
        # test shards actually ran.
        assert wire["census_status"] == EnumFactStatus.FAIL.value
        assert wire["census_original_status"] == EnumFactStatus.FAIL.value
        assert wire["health_status"] == EnumFactStatus.WARN.value


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_lane_outside_the_lab_never_reaches_the_table() -> None:
    """AC6, proven at the storage layer rather than only at the parser."""
    async with _migrated_handler() as (handler, connection, schema):
        await handler.project_event(
            TOPIC_RUNTIME_HEALTH,
            {
                "lane": "stability-test",
                "timestamp": NOW.isoformat(),
                "status": "HEALTHY",
                "dimensions": [],
            },
            _MessageMeta(),
        )

        count = await connection.fetchval(
            f"SELECT count(*) FROM {schema}.lab_lane_health"
        )
        assert count == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_runtime_injected_entry_writes_a_row_against_real_postgres() -> None:
    """The entry the projection wiring path actually calls must WRITE.

    This is the regression that made the node consume, commit its offsets and
    store nothing while every observability surface read healthy. The pure
    definition-B handler validated and returned; nothing reached the database.

    So this drives the entry the runtime invokes, with the injections the
    runtime supplies -- ``_topic`` popped from the payload rather than read as
    a field of the event -- and asserts a row exists afterwards. A mock
    database cannot stand in here: the column types are what turn a str-vs-
    datetime fold bug into a failure (OMN-15905), which is why the write-path
    gate demands a real DSN.
    """
    async with _migrated_handler() as (writer, connection, schema):
        injected = {
            "lane": "compose-dev",
            "timestamp": NOW.isoformat(),
            "status": "DEGRADED",
            "dimensions": [{"name": "consumer_groups", "status": "DEGRADED"}],
            # The runtime's own injections, exactly as handler_wiring adds them.
            "_topic": TOPIC_RUNTIME_HEALTH,
            "_partition": 0,
            "_offset": 41,
        }

        result = writer.handle(injected)

        assert result["applied"] is True
        assert result["topic"] == TOPIC_RUNTIME_HEALTH
        # `_topic` must be consumed as metadata, never folded as event data.
        assert "_topic" not in injected

        stored = await connection.fetchval(
            f"SELECT count(*) FROM {schema}.lab_lane_health WHERE lane = $1",
            "compose-dev",
        )
        assert stored == 1
