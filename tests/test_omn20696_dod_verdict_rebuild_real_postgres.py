# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real-Postgres proof that the DoD verdict table rebuilds from its events.

OMN-20696, split from OR.3 (OMN-20071), plan step S4. The unit module proves
the contract subject survives command, state, wire and fold, and that the fold
rebuilds the same rows from any replay order. This proves the durable half on
a real database:

* the real writer's projection path stores the subject in its typed columns;
* dropping every row and replaying the same events -- shuffled, with
  redeliveries -- leaves a table whose content digest equals the original's;
* the table itself refuses a row that names a repository-owned source without
  the commit it was read at, so no writer can store an unbound verdict as a
  bound one.

THE RED HALF IS MECHANICAL. Without migration 0004 the upsert names columns
the table does not have and raises UndefinedColumn.
"""

from __future__ import annotations

import hashlib
import json
import random
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import asyncpg
import pytest

from omnimarket.nodes.node_projection_dod_verdict.handlers import (
    handler_dod_verdict_runner as writer_module,
)
from omnimarket.nodes.node_projection_dod_verdict.handlers.handler_dod_verdict_runner import (
    DodVerdictProjectionWriter,
)
from tests.test_omn19514_dod_verdict_delegation_run_real_postgres import (
    _ConnectionDb,
)
from tests.test_omn19514_ticket_id_projection_real_postgres import (
    _Postgres,
)
from tests.test_omn19514_ticket_id_projection_real_postgres import (
    postgres as postgres,
)

# Both forms deliberately: the module mark is what pytest selects on, and the
# per-test decorator is what scripts/ci/check_projection_write_path_db_gate.py
# reads to confirm a write-path change brought a real-Postgres test with it.
pytestmark = pytest.mark.integration

_MIGRATIONS = Path(__file__).resolve().parents[1] / (
    "src/omnimarket/nodes/node_projection_dod_verdict/migrations"
)
#: 0001 is a grant to a role a throwaway schema does not carry.
MIGRATIONS = (
    _MIGRATIONS / "0000_create_dod_verify_runs.sql",
    _MIGRATIONS / "0002_dod_verify_runs_delegation_correlation_id.sql",
    _MIGRATIONS / "0003_dod_verify_runs_goal.sql",
    _MIGRATIONS / "0004_dod_verify_runs_contract_subject.sql",
)

_STARTED = datetime(2026, 10, 7, 10, 0, tzinfo=UTC)
_PRODUCT_SHA = "0123456789abcdef0123456789abcdef01234567"


@asynccontextmanager
async def _migrated_writer(
    monkeypatch: pytest.MonkeyPatch,
    dsn: str,
) -> AsyncIterator[tuple[DodVerdictProjectionWriter, asyncpg.Connection, str]]:
    """A throwaway schema carrying the real migrations, wired to the real writer."""
    connection = await asyncpg.connect(dsn)
    schema = f"omn20696_{uuid4().hex[:12]}"
    original_table = writer_module.TABLE
    original_upsert = writer_module._UPSERT
    try:
        await connection.execute(f"CREATE SCHEMA {schema}")
        for migration in MIGRATIONS:
            ddl = migration.read_text().replace("omninode_internal.", f"{schema}.")
            await connection.execute(ddl)

        writer = DodVerdictProjectionWriter()
        monkeypatch.setattr(writer, "_db", _ConnectionDb(connection))
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
    base: dict[str, Any] = {
        "correlation_id": str(uuid4()),
        "ticket_id": "OMN-20696",
        "status": "verified",
        "started_at": _STARTED.isoformat(),
        "completed_at": (_STARTED + timedelta(minutes=2)).isoformat(),
        "total_checks": 3,
        "verified_count": 3,
        "behavior_proving_count": 1,
        "contract_source": "product_repository",
        "contract_repository": "OmniNode-ai/omnimarket",
        "contract_commit_sha": _PRODUCT_SHA,
        "contract_repo_path": "contracts/OMN-20696.yaml",
    }
    base.update(overrides)
    return base


def _history() -> list[dict[str, Any]]:
    shared = str(uuid4())
    return [
        _event(status="failed", failed_count=1, verified_count=2),
        _event(
            completed_at=(_STARTED + timedelta(minutes=9)).isoformat(),
            contract_commit_sha="f" * 40,
        ),
        _event(
            correlation_id=shared,
            completed_at=(_STARTED + timedelta(minutes=20)).isoformat(),
        ),
        _event(
            correlation_id=shared,
            completed_at=(_STARTED + timedelta(minutes=30)).isoformat(),
        ),
        _event(
            ticket_id="OMN-20070",
            contract_source="onex_change_control",
            contract_repository="OmniNode-ai/onex_change_control",
            contract_commit_sha="e" * 40,
            contract_repo_path="contracts/OMN-20070.yaml",
        ),
        # An event from before this change: no subject at all.
        {
            key: value
            for key, value in _event(ticket_id="OMN-18900").items()
            if not key.startswith("contract_")
        },
        _event(dry_run=True),
    ]


async def _write(writer: DodVerdictProjectionWriter, payload: dict[str, Any]) -> None:
    """Fold and persist one event through the writer's own projection path."""
    await writer._project_verdict(dict(payload))


async def _digest(connection: asyncpg.Connection, schema: str) -> tuple[int, str]:
    """Content digest of every column but the two the database itself assigns.

    ``projected_at`` is the reducer's wall clock and the surrogate cursor is a
    sequence, so neither is a fact about any run; every other column is.
    """
    rows = await connection.fetch(
        f"SELECT * FROM {schema}.dod_verify_runs "
        "ORDER BY ticket_id, correlation_id, completed_at"
    )
    excluded = {"projected_at", "cursor", "id", "projection_cursor"}
    body = [
        {key: str(value) for key, value in dict(row).items() if key not in excluded}
        for row in rows
    ]
    encoded = json.dumps(body, sort_keys=True).encode()
    return len(rows), hashlib.sha256(encoded).hexdigest()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_subject_reaches_its_typed_columns(
    monkeypatch: pytest.MonkeyPatch,
    postgres: _Postgres,
) -> None:
    async with _migrated_writer(monkeypatch, postgres.dsn("public")) as (
        writer,
        connection,
        schema,
    ):
        await _write(writer, _event())
        row = await connection.fetchrow(
            f"SELECT contract_source, contract_repository, "
            f"contract_commit_sha, contract_repo_path "
            f"FROM {schema}.dod_verify_runs"
        )
        assert row is not None
        assert dict(row) == {
            "contract_source": "product_repository",
            "contract_repository": "OmniNode-ai/omnimarket",
            "contract_commit_sha": _PRODUCT_SHA,
            "contract_repo_path": "contracts/OMN-20696.yaml",
        }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dropping_the_rows_and_replaying_the_events_rebuilds_the_same_table(
    monkeypatch: pytest.MonkeyPatch,
    postgres: _Postgres,
) -> None:
    async with _migrated_writer(monkeypatch, postgres.dsn("public")) as (
        writer,
        connection,
        schema,
    ):
        history = _history()
        for payload in history:
            await _write(writer, payload)
        original = await _digest(connection, schema)
        assert original[0] == 6  # the rehearsal is not an attempt

        rng = random.Random(20696)
        for _ in range(3):
            await connection.execute(f"TRUNCATE {schema}.dod_verify_runs")
            replay = history + rng.sample(history, k=3)
            rng.shuffle(replay)
            for payload in replay:
                await _write(writer, payload)
            assert await _digest(connection, schema) == original


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_table_refuses_a_bound_source_without_its_commit(
    monkeypatch: pytest.MonkeyPatch,
    postgres: _Postgres,
) -> None:
    insert = (
        "INSERT INTO {schema}.dod_verify_runs ("
        "ticket_id, correlation_id, completed_at, started_at, status, "
        "total_checks, verified_count, failed_count, skipped_count, "
        "superseded_count, non_probative_count, behavior_proving_count, "
        "readback_proving_count, unbindable_overlay_count, outcome, "
        "error_message, projected_at, contract_source, "
        "contract_repository, contract_commit_sha, contract_repo_path) VALUES ("
        "'OMN-20696', $1, now(), now(), 'verified', 1, 1, 0, 0, 0, 0, "
        "1, 0, 0, 'done', '', now(), 'product_repository', "
        "'OmniNode-ai/omnimarket', $2, 'contracts/OMN-20696.yaml')"
    )
    async with _migrated_writer(monkeypatch, postgres.dsn("public")) as (
        _writer,
        connection,
        schema,
    ):
        with pytest.raises(asyncpg.CheckViolationError):
            await connection.execute(insert.format(schema=schema), uuid4(), None)
        # The same row WITH its commit is accepted, so the refusal above is
        # the subject constraint and not some other column's.
        await connection.execute(insert.format(schema=schema), uuid4(), _PRODUCT_SHA)
        with pytest.raises(asyncpg.CheckViolationError):
            await connection.execute(insert.format(schema=schema), uuid4(), "abc1234")
