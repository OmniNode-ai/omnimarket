# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real-Postgres write-path gate for the DoD verdict projection (OMN-18900).

WHY THIS EXISTS BESIDE THE GOLDEN CHAIN
    A database double accepts an ISO string bound to a TIMESTAMPTZ, and a
    string bound to a UUID, exactly as readily as a real ``datetime`` or a
    real ``UUID``. That is the one question a double cannot answer, and it is
    the gap that took a crash-looping runtime to production with every mock
    green (OMN-15905). This projection binds three TIMESTAMPTZ columns and a
    UUID column, so it is squarely in that class.

    Real Postgres, never SQLite: the upsert's conflict target, the BIGSERIAL
    cursor's own sequence and the enum-as-TEXT columns all behave differently
    on anything else, so a substitute would prove something about the
    substitute.

The migration is applied into a throwaway ``uuid4``-named schema so
concurrent runs never collide, and the test SKIPS rather than errors when no
server is reachable.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus
from uuid import UUID, uuid4

import asyncpg
import pytest

from omnimarket.enums.enum_dod_verify_status import EnumDodVerifyStatus
from omnimarket.nodes.node_projection_dod_verdict.handlers import (
    handler_dod_verdict_runner as writer_module,
)
from omnimarket.nodes.node_projection_dod_verdict.handlers.handler_dod_verdict_runner import (
    DodVerdictProjectionWriter,
)
from omnimarket.nodes.node_projection_dod_verdict.models import (
    EnumDodEvalOutcome,
    EnumDodEvalRefusal,
)

# Both forms deliberately: the module mark is what pytest selects on, and the
# per-test decorator is what scripts/ci/check_projection_write_path_db_gate.py
# reads to confirm a write-path change brought a real-Postgres test with it.
pytestmark = pytest.mark.integration

MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_projection_dod_verdict/migrations"
    / "0000_create_dod_verify_runs.sql"
)
#: OMN-19514: the writer names delegation_correlation_id, which 0002 adds.
DELEGATION_RUN_MIGRATION = MIGRATION.with_name(
    "0002_dod_verify_runs_delegation_correlation_id.sql"
)

CORRELATION = UUID("d4396b48-e783-4523-98b1-5f795b5f7b51")
STARTED = datetime(2026, 9, 20, 11, 0, tzinfo=UTC)
COMPLETED = datetime(2026, 9, 20, 11, 4, tzinfo=UTC)


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
            "POSTGRES_PASSWORD not set -- skipping the OMN-18900 real-Postgres "
            "DoD verdict write-path gate"
        )
        raise AssertionError("unreachable: pytest.skip always raises")
    try:
        return await asyncpg.connect(_base_dsn())
    except (OSError, asyncpg.PostgresError) as exc:  # pragma: no cover - infra
        pytest.skip(f"no reachable Postgres for the OMN-18900 write-path gate: {exc}")
        raise AssertionError("unreachable: pytest.skip always raises") from exc


class _ConnectionDb:
    """The two methods the writer calls, bound to one disposable connection.

    A pooled adapter would not keep the throwaway schema isolated, and the
    writer must not assume it owns the adapter it was handed -- the
    connect/close bracket it opens per message is honoured here and does
    nothing, which is the point.
    """

    def __init__(self, connection: asyncpg.Connection) -> None:
        self._connection = connection

    async def execute(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        rows = await self._connection.fetch(sql, *args)
        return [dict(row) for row in rows]

    async def fetchval(self, sql: str, *args: Any) -> Any:
        return await self._connection.fetchval(sql, *args)

    async def connect(self) -> None:
        """No-op: already bound to one live connection."""

    async def close(self) -> None:
        """No-op, for the same reason as :meth:`connect`."""


@asynccontextmanager
async def _migrated_writer() -> AsyncIterator[
    tuple[DodVerdictProjectionWriter, asyncpg.Connection, str]
]:
    """A throwaway schema carrying the real migration, wired to the real writer."""
    connection = await _connect_or_skip()
    schema = f"omn18900_{uuid4().hex[:12]}"
    original_table = writer_module.TABLE
    original_upsert = writer_module._UPSERT
    try:
        await connection.execute(f"CREATE SCHEMA {schema}")
        for migration in (MIGRATION, DELEGATION_RUN_MIGRATION):
            ddl = migration.read_text().replace("omninode_internal.", f"{schema}.")
            await connection.execute(ddl)

        writer = DodVerdictProjectionWriter()
        writer._db = _ConnectionDb(connection)  # type: ignore[assignment]

        # Point the writer's SQL at the disposable schema. The statement is
        # formatted from one TABLE constant, so rebinding both rewrites every
        # occurrence consistently rather than per call site.
        writer_module.TABLE = f"{schema}.dod_verify_runs"
        writer_module._UPSERT = original_upsert.replace(
            "omninode_internal.dod_verify_runs", f"{schema}.dod_verify_runs"
        )
        yield writer, connection, schema
    finally:
        writer_module.TABLE = original_table
        writer_module._UPSERT = original_upsert
        await connection.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await connection.close()


def _event(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "correlation_id": str(CORRELATION),
        "ticket_id": "OMN-17372",
        "status": "verified",
        "started_at": STARTED.isoformat(),
        "completed_at": COMPLETED.isoformat(),
        "total_checks": 88,
        "verified_count": 16,
        "failed_count": 0,
        "skipped_count": 0,
        "superseded_count": 0,
        "non_probative_count": 72,
        "behavior_proving_count": 0,
        "readback_proving_count": 0,
        "unbindable_overlay_count": 0,
    }
    payload.update(overrides)
    return payload


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_row_lands_with_typed_column_values() -> None:
    """The write reaches real Postgres and reads back with real types.

    ``completed_at`` comes back a ``datetime`` and ``correlation_id`` a
    ``UUID``. A double would have accepted the ISO strings the event carries
    and this assertion is the only place that distinction is made.
    """
    async with _migrated_writer() as (writer, connection, schema):
        row = await writer._project_verdict(_event())
        assert row is not None

        stored = await connection.fetchrow(
            f"SELECT * FROM {schema}.dod_verify_runs WHERE ticket_id = $1",
            "OMN-17372",
        )
        assert stored is not None
        assert isinstance(stored["completed_at"], datetime)
        assert stored["completed_at"] == COMPLETED
        assert isinstance(stored["started_at"], datetime)
        assert isinstance(stored["correlation_id"], UUID)
        assert stored["correlation_id"] == CORRELATION
        assert isinstance(stored["projected_at"], datetime)
        assert stored["projection_cursor"] >= 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_every_class_count_is_stored_field_by_field() -> None:
    """The counts survive the round trip one column at a time."""
    async with _migrated_writer() as (writer, connection, schema):
        await writer._project_verdict(_event())
        stored = await connection.fetchrow(
            f"SELECT * FROM {schema}.dod_verify_runs WHERE ticket_id = $1",
            "OMN-17372",
        )
        assert stored is not None
        assert stored["total_checks"] == 88
        assert stored["verified_count"] == 16
        assert stored["failed_count"] == 0
        assert stored["skipped_count"] == 0
        assert stored["superseded_count"] == 0
        assert stored["non_probative_count"] == 72
        assert stored["behavior_proving_count"] == 0
        assert stored["status"] == EnumDodVerifyStatus.VERIFIED.value
        assert stored["outcome"] == EnumDodEvalOutcome.REFUSED.value
        assert (
            stored["outcome_refusal"]
            == EnumDodEvalRefusal.NO_BEHAVIOR_PROVING_CHECK.value
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_redelivery_converges_and_a_reverification_adds_a_row() -> None:
    """The three-column key does what the contract's dedupe key declares.

    The same verdict twice is ONE row; a later completion time is a second.
    Both halves matter: without the first a redelivery would duplicate the
    run, and without the second the metric could not count attempts.
    """
    async with _migrated_writer() as (writer, connection, schema):
        await writer._project_verdict(_event())
        await writer._project_verdict(_event())

        count = await connection.fetchval(
            f"SELECT count(*) FROM {schema}.dod_verify_runs"
        )
        assert count == 1

        later = datetime(2026, 9, 20, 15, 30, tzinfo=UTC)
        await writer._project_verdict(
            _event(completed_at=later.isoformat(), behavior_proving_count=3)
        )

        rows = await connection.fetch(
            f"SELECT completed_at, outcome, outcome_refusal "
            f"FROM {schema}.dod_verify_runs ORDER BY completed_at"
        )
        assert len(rows) == 2
        assert rows[0]["outcome"] == EnumDodEvalOutcome.REFUSED.value
        assert rows[1]["outcome"] == EnumDodEvalOutcome.DONE.value
        assert rows[1]["outcome_refusal"] is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_failing_and_an_unresolved_verdict_both_land() -> None:
    """Every terminal status is durable, proven against the real column types.

    ``unresolved_cause`` is NULL on the failing row and set on the unresolved
    one, which is the pairing the producing models enforce and the shape a
    reader has to be able to tell apart.
    """
    async with _migrated_writer() as (writer, connection, schema):
        await writer._project_verdict(
            _event(status="failed", failed_count=2, verified_count=13, skipped_count=1)
        )
        await writer._project_verdict(
            _event(
                status="unresolved",
                unresolved_cause="run_error_or_timeout",
                completed_at=datetime(2026, 9, 20, 12, 0, tzinfo=UTC).isoformat(),
                total_checks=0,
                verified_count=0,
                non_probative_count=0,
            )
        )

        rows = await connection.fetch(
            f"SELECT status, unresolved_cause, outcome, outcome_refusal "
            f"FROM {schema}.dod_verify_runs ORDER BY completed_at"
        )
        assert len(rows) == 2
        assert rows[0]["status"] == EnumDodVerifyStatus.FAILED.value
        assert rows[0]["unresolved_cause"] is None
        assert rows[0]["outcome_refusal"] == EnumDodEvalRefusal.CHECKS_FAILED.value
        assert rows[1]["status"] == EnumDodVerifyStatus.UNRESOLVED.value
        assert rows[1]["unresolved_cause"] == "run_error_or_timeout"
        assert (
            rows[1]["outcome_refusal"] == EnumDodEvalRefusal.STATUS_NOT_VERIFIED.value
        )
