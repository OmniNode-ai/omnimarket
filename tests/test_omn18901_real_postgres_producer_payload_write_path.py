# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real-Postgres gate for the producer payload reaching the verdict row (OMN-18901).

WHY THIS EXISTS BESIDE THE UNIT SUITE
    The unit suite drives the real writer over a recording double. A double
    accepts an ISO string bound to a TIMESTAMPTZ and a string bound to a UUID
    exactly as readily as a real ``datetime`` or ``UUID`` (OMN-15905), so it
    cannot answer the one question this change actually raises: the run window
    this ticket put on ``ModelDodVerifyState`` is serialised by the runtime and
    bound straight into two TIMESTAMPTZ columns, and nothing short of a real
    connection enforces that.

    Sibling OMN-18900 already proves the write path against a hand-written
    payload. This module proves it against the payload the PRODUCER actually
    emits -- ``ModelDodVerifyState.model_dump(mode="json")``, taken off a live
    run of the verify path rather than typed into a fixture. A fixture would
    keep passing after the producing model moved, which is precisely the
    failure that left the merged projection with a table and no rows.

    It also carries the write-path half of the rehearsal rule: a dry-run
    verdict must leave the table untouched, and the assertion for that is
    ``SELECT count(*)`` against real Postgres, not a mock that was never
    called.

The migration is applied into a throwaway ``uuid4``-named schema so concurrent
runs never collide, and every test SKIPS rather than errors when no server is
reachable.
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

from omnimarket.nodes.node_dod_verify.handlers.handler_dod_verify import (
    HandlerDodVerify,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_start_command import (
    ModelDodVerifyStartCommand,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumDodVerifyStatus,
    EnumEvidenceCheckStatus,
    ModelDodVerifyState,
    ModelEvidenceCheckResult,
)
from omnimarket.nodes.node_projection_dod_verdict.handlers import (
    handler_dod_verdict_runner as writer_module,
)
from omnimarket.nodes.node_projection_dod_verdict.handlers.handler_dod_verdict_runner import (
    DodVerdictProjectionWriter,
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
            "POSTGRES_PASSWORD not set -- skipping the OMN-18901 real-Postgres "
            "producer-payload write-path gate"
        )
        raise AssertionError("unreachable: pytest.skip always raises")
    try:
        return await asyncpg.connect(_base_dsn())
    except (OSError, asyncpg.PostgresError) as exc:  # pragma: no cover - infra
        pytest.skip(f"no reachable Postgres for the OMN-18901 write-path gate: {exc}")
        raise AssertionError("unreachable: pytest.skip always raises") from exc


class _ConnectionDb:
    """The two methods the writer calls, bound to one disposable connection."""

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
    schema = f"omn18901_{uuid4().hex[:12]}"
    original_table = writer_module.TABLE
    original_upsert = writer_module._UPSERT
    try:
        await connection.execute(f"CREATE SCHEMA {schema}")
        ddl = MIGRATION.read_text().replace("omninode_internal.", f"{schema}.")
        await connection.execute(ddl)

        writer = DodVerdictProjectionWriter()
        writer._db = _ConnectionDb(connection)  # type: ignore[assignment]

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


def _check(
    evidence_id: str,
    status: EnumEvidenceCheckStatus,
    message: str | None = None,
) -> ModelEvidenceCheckResult:
    return ModelEvidenceCheckResult(
        evidence_id=evidence_id,
        description=f"check {evidence_id}",
        status=status,
        message=message,
    )


def _produced_payload(
    *,
    ticket_id: str,
    checks: list[ModelEvidenceCheckResult],
    dry_run: bool = False,
) -> dict[str, Any]:
    """Run the REAL verify path and return what the runtime would publish.

    The runtime wraps a definition-B handler's returned ``BaseModel`` as an
    output event and serialises it onto the contract's terminal topic, so this
    dump is the wire payload. Deriving it from a live run rather than writing
    it out by hand is the entire point of this module.
    """
    state = HandlerDodVerify().handle(
        ModelDodVerifyStartCommand(
            correlation_id=uuid4(),
            ticket_id=ticket_id,
            dry_run=dry_run,
            requested_at=datetime.now(tz=UTC),
        ),
        evidence_results=checks,
    )
    assert isinstance(state, ModelDodVerifyState)
    return dict(state.model_dump(mode="json"))


async def _row_count(connection: asyncpg.Connection, schema: str) -> int:
    count = await connection.fetchval(f"SELECT count(*) FROM {schema}.dod_verify_runs")
    assert isinstance(count, int)
    return count


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_producer_payload_lands_with_typed_timestamp_columns() -> None:
    """The run window the producer now carries binds as real TIMESTAMPTZ values.

    Before this ticket ``ModelDodVerifyState`` carried no ``started_at`` or
    ``completed_at`` at all, so the payload the runtime published could only be
    rejected by the merged wire model. This asserts the positive direction
    against real column types: the two timestamps read back as ``datetime``
    and the correlation id as ``UUID``, none of which a double could have
    distinguished from the ISO strings the payload actually carries.
    """
    payload = _produced_payload(
        ticket_id="OMN-18901",
        checks=[
            _check("dod-001", EnumEvidenceCheckStatus.VERIFIED),
            _check("dod-002", EnumEvidenceCheckStatus.VERIFIED),
        ],
    )
    assert isinstance(payload["started_at"], str)
    assert isinstance(payload["completed_at"], str)

    async with _migrated_writer() as (writer, connection, schema):
        row = await writer._project_verdict(payload)
        assert row is not None

        stored = await connection.fetchrow(
            f"SELECT * FROM {schema}.dod_verify_runs WHERE ticket_id = $1",
            "OMN-18901",
        )
        assert stored is not None
        assert isinstance(stored["started_at"], datetime)
        assert isinstance(stored["completed_at"], datetime)
        assert stored["started_at"] <= stored["completed_at"]
        assert isinstance(stored["correlation_id"], UUID)
        assert str(stored["correlation_id"]) == payload["correlation_id"]
        assert stored["status"] == EnumDodVerifyStatus.VERIFIED.value


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_failed_verdict_is_as_durable_as_a_passing_one() -> None:
    """The positive control for AC-2, taken against the table rather than a mock.

    A verdict surface that exists only on success cannot tell a failure from an
    unrun verification, which is the whole reason the table exists. The failing
    run is produced by the same verify path as the passing one above, so this
    is a statement about the producer, not about a fixture.
    """
    payload = _produced_payload(
        ticket_id="OMN-18901",
        checks=[
            _check("dod-001", EnumEvidenceCheckStatus.VERIFIED),
            _check("dod-002", EnumEvidenceCheckStatus.FAILED, "assertion did not hold"),
        ],
    )
    assert payload["status"] == EnumDodVerifyStatus.FAILED.value
    assert payload["failed_count"] == 1

    async with _migrated_writer() as (writer, connection, schema):
        assert await _row_count(connection, schema) == 0
        row = await writer._project_verdict(payload)
        assert row is not None

        stored = await connection.fetchrow(
            f"SELECT * FROM {schema}.dod_verify_runs WHERE ticket_id = $1",
            "OMN-18901",
        )
        assert stored is not None
        assert stored["status"] == EnumDodVerifyStatus.FAILED.value
        assert stored["failed_count"] == 1
        assert stored["verified_count"] == 1
        assert await _row_count(connection, schema) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_rehearsal_leaves_the_table_empty_and_a_real_run_does_not() -> None:
    """The dry-run refusal, proven by counting rows rather than calls.

    Both halves in one schema so the zero is not an artifact of a write path
    that was broken for every payload: the rehearsal writes nothing, and the
    same verify path without the flag writes exactly one row immediately
    afterwards.
    """
    rehearsal = _produced_payload(
        ticket_id="OMN-18901",
        checks=[_check("dod-001", EnumEvidenceCheckStatus.VERIFIED)],
        dry_run=True,
    )
    assert rehearsal["dry_run"] is True

    real = _produced_payload(
        ticket_id="OMN-18901",
        checks=[_check("dod-001", EnumEvidenceCheckStatus.VERIFIED)],
    )
    assert real["dry_run"] is False

    async with _migrated_writer() as (writer, connection, schema):
        assert await writer._project_verdict(rehearsal) is None
        assert await _row_count(connection, schema) == 0

        assert await writer._project_verdict(real) is not None
        assert await _row_count(connection, schema) == 1
