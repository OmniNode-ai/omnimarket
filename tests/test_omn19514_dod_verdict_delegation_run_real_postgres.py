# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real-Postgres proof that a DoD verdict row names the delegation it judged.

OMN-19514. The unit module proves the field survives command, state, wire and
fold. This proves the write path: the producer's own payload, taken off a live
run of the verify path, binds into a real UUID column through the real writer
and reads back as the delegation's correlation id, and a verdict that named no
delegation stores NULL. The join this exists for is read back at the end.

THE RED HALF IS MECHANICAL. Without migration 0002 the upsert names a column
the table does not have and raises UndefinedColumn.
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

_MIGRATIONS = Path(__file__).resolve().parents[1] / (
    "src/omnimarket/nodes/node_projection_dod_verdict/migrations"
)
#: The create and this ticket's column. 0001 is a grant to a role a throwaway
#: schema does not carry, and grants are not what this module proves.
MIGRATIONS = (
    _MIGRATIONS / "0000_create_dod_verify_runs.sql",
    _MIGRATIONS / "0002_dod_verify_runs_delegation_correlation_id.sql",
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
            "POSTGRES_PASSWORD not set -- skipping the OMN-19514 real-Postgres "
            "producer-payload write-path gate"
        )
        raise AssertionError("unreachable: pytest.skip always raises")
    try:
        return await asyncpg.connect(_base_dsn())
    except (OSError, asyncpg.PostgresError) as exc:  # pragma: no cover - infra
        pytest.skip(f"no reachable Postgres for the OMN-19514 write-path gate: {exc}")
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
    schema = f"omn19514_{uuid4().hex[:12]}"
    original_table = writer_module.TABLE
    original_upsert = writer_module._UPSERT
    try:
        await connection.execute(f"CREATE SCHEMA {schema}")
        for migration in MIGRATIONS:
            ddl = migration.read_text().replace("omninode_internal.", f"{schema}.")
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
    delegation_correlation_id: UUID | None = None,
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
            delegation_correlation_id=delegation_correlation_id,
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
async def test_a_linked_verdict_stores_the_delegation_run() -> None:
    delegation = uuid4()
    payload = _produced_payload(
        ticket_id="OMN-19514",
        checks=[_check("dod-001", EnumEvidenceCheckStatus.VERIFIED)],
        delegation_correlation_id=delegation,
    )
    assert payload["delegation_correlation_id"] == str(delegation)
    async with _migrated_writer() as (writer, connection, schema):
        assert await writer._project_verdict(payload) is not None
        stored = await connection.fetchval(
            f"SELECT delegation_correlation_id FROM {schema}.dod_verify_runs "
            "WHERE ticket_id = $1",
            "OMN-19514",
        )
    assert isinstance(stored, UUID)
    assert stored == delegation


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_unlinked_verdict_stores_null() -> None:
    payload = _produced_payload(
        ticket_id="OMN-19514",
        checks=[_check("dod-001", EnumEvidenceCheckStatus.VERIFIED)],
    )
    assert "delegation_correlation_id" not in payload
    async with _migrated_writer() as (writer, connection, schema):
        assert await writer._project_verdict(payload) is not None
        count = await connection.fetchval(
            f"SELECT count(*) FROM {schema}.dod_verify_runs "
            "WHERE delegation_correlation_id IS NULL"
        )
    assert count == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_verdict_joins_to_the_delegation_row_it_judged() -> None:
    """The join the column exists for, against a stand-in delegation_events."""
    delegation = uuid4()
    payload = _produced_payload(
        ticket_id="OMN-19514",
        checks=[_check("dod-001", EnumEvidenceCheckStatus.VERIFIED)],
        delegation_correlation_id=delegation,
    )
    async with _migrated_writer() as (writer, connection, schema):
        await connection.execute(
            f"CREATE TABLE {schema}.delegation_events "
            "(correlation_id UUID PRIMARY KEY, ticket_id TEXT)"
        )
        await connection.execute(
            f"INSERT INTO {schema}.delegation_events VALUES ($1, $2), ($3, $4)",
            delegation,
            "OMN-19514",
            uuid4(),
            "OMN-19514",
        )
        assert await writer._project_verdict(payload) is not None
        joined = await connection.fetch(
            f"SELECT d.correlation_id, d.ticket_id, v.status "
            f"FROM {schema}.delegation_events d "
            f"JOIN {schema}.dod_verify_runs v "
            "ON v.delegation_correlation_id = d.correlation_id "
            "AND v.ticket_id = d.ticket_id"
        )
    assert [(row["correlation_id"], row["ticket_id"]) for row in joined] == [
        (delegation, "OMN-19514")
    ]
