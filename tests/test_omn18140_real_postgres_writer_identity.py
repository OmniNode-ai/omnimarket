# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18140: real-Postgres proof that ``delegation_events`` carries a writer
attestation the writing process cannot forge.

WHY A REAL DATABASE IS REQUIRED HERE, and why the statement-shape module
``tests/test_omn18140_delegation_writer_attestation.py`` is not enough. That
module asserts what the writer PROPOSES -- that ``CURRENT_USER`` and ``NOW()``
reach the statement as expressions rather than bound parameters. It cannot
observe the only thing that matters about them: that POSTGRES evaluates them,
per row, against the connection actually in use. An ``AsyncMock`` accepts any
statement text and returns whatever it is told to, so a writer that composed a
syntactically perfect statement against a column that does not exist would pass
every assertion in that module. Only a real connection can fail on the column,
and only a real connection can prove the stamped value equals the session's own
principal.

THE RED HALF IS THE PARENT COMMIT'S SCHEMA, BUILT MECHANICALLY.
``TestTheSchemaWithoutTheMigrationRefusesTheWrite`` applies the migration chain
with ``0038`` EXCLUDED -- which is exactly the tree at this branch's parent --
and asserts the live writer fails with ``UndefinedColumnError`` naming
``writer_identity``. That is the RED measurement, reproducible in one run rather
than asserted from a commit nobody can re-execute later, and it is what makes
the GREEN half below evidence instead of a tautology. A positive control
(``TestTheFixtureIsFaithful``) proves the excluded-migration schema really is
missing the column before any behavioural assertion runs on it, so a fixture
that silently applied 0038 anyway cannot green the red half.

WHAT THIS MODULE DOES NOT PROVE, stated rather than left to be discovered. The
value ``CURRENT_USER`` resolves to is the identity of whatever connection the
test opens -- here, the throwaway container's superuser. It is NOT evidence
about which principal the deployed writer connects as on any lane. That is a
deployment fact, readable only from the running plane, and the staging-green
bar's leg-4 readback is the instrument for it. What is proven here is the
mechanism: the column records the connection's own principal, per row, on both
arms of the upsert, and no value supplied by the writing process can displace
it.

Harness (``_connect_or_skip`` / disposable schema / guarded ``app_dashboard``
role) mirrors ``tests/test_omn17228_real_postgres_drifted_default_write_path.py``
in spirit: SKIPS rather than ERRORs without a reachable database, and provisions
a throwaway schema so concurrent runs never collide.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock
from urllib.parse import quote_plus
from uuid import uuid4

import asyncpg
import pytest
import yaml

from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter
from omnimarket.events.topics import TASK_DELEGATED_TOPIC_V1
from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation import (
    DelegationProjectionRunner,
)
from omnimarket.projection.discovery import load_projection_exposures_from_contract
from omnimarket.projection.envelope import unwrap_envelope
from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.runner import MessageMeta

_NODE_ROOT = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_delegation"
)
_MIGRATIONS_DIR = _NODE_ROOT / "migrations"
_CONTRACT_PATH = _NODE_ROOT / "contract.yaml"

#: The migration under test. Excluding exactly this file reproduces the parent
#: commit's schema without checking it out.
_MIGRATION_UNDER_TEST = "0038_delegation_events_writer_identity.sql"

_ATTESTATION_COLUMNS = ("writer_identity", "written_at")

_ENVELOPE_TIMESTAMP = datetime(2026, 9, 10, 5, 1, 33, 932000, tzinfo=UTC)
_TENANT_UUID = "820272f9-4aaf-5add-a2df-0af942852ab2"
_TENANT_SLUG = "omninode"

_TENANT_REGISTRY_MIRROR_SQL = (
    (
        Path(__file__).resolve().parents[1]
        / "src"
        / "omnimarket"
        / "nodes"
        / "node_projection_tenant_registry"
        / "migrations"
        / "0000_create_tenant_registry_mirror.sql"
    )
    .read_text(encoding="utf-8")
    .replace("CREATE INDEX CONCURRENTLY", "CREATE INDEX")
)

_APP_DASHBOARD_ROLE_SQL = """
DO $$
BEGIN
  BEGIN
    CREATE ROLE app_dashboard WITH
      NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION;
  EXCEPTION
    WHEN duplicate_object OR unique_violation THEN
      NULL;
  END;
END;
$$;
"""


def _row_exposure() -> ProjectionTableConfig:
    contract = yaml.safe_load(_CONTRACT_PATH.read_text(encoding="utf-8"))
    scoped = [
        exposure
        for exposure in load_projection_exposures_from_contract(
            contract, "projection_delegation", _CONTRACT_PATH
        )
        if exposure.table == "delegation_events"
        and exposure.bus_backed
        and exposure.tenant_scoped
    ]
    assert len(scoped) == 1
    return scoped[0]


def _live_migration_files(*, exclude: str | None = None) -> list[Path]:
    return [
        path
        for path in sorted(_MIGRATIONS_DIR.glob("*.sql"))
        if exclude is None or path.name != exclude
    ]


def _test_schema_safe_sql(raw_sql: str) -> str:
    """``CONCURRENTLY`` refuses to run inside the implicit transaction asyncpg's
    multi-statement ``execute()`` opens. It exists only to avoid locking a live
    table, which a disposable schema does not have."""
    return raw_sql.replace("CREATE INDEX CONCURRENTLY", "CREATE INDEX").replace(
        "CREATE UNIQUE INDEX CONCURRENTLY", "CREATE UNIQUE INDEX"
    )


def _base_dsn() -> str:
    secret = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = os.environ.get("INTEGRATION_POSTGRES_PORT", "5432")
    user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
    db = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
    return f"postgresql://{quote_plus(user)}:{quote_plus(secret)}@{host}:{port}/{db}"


async def _connect_or_skip() -> asyncpg.Connection:
    if not os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    ):
        pytest.skip(
            "POSTGRES_PASSWORD not set -- skipping the OMN-18140 writer-attestation "
            "gate"
        )
    try:
        return await asyncpg.connect(_base_dsn())
    except (OSError, asyncpg.PostgresError) as exc:  # pragma: no cover - infra
        pytest.skip(f"no reachable Postgres for the OMN-18140 gate: {exc}")


async def _capture(topic: str, value: bytes) -> None:
    """The runner's ``publish_fn`` seam -- without one, ``get_publish_fn``
    builds a real ``AIOKafkaProducer`` and blocks on connect retries."""
    return


def _intercept_snapshot_sends(
    runner: DelegationProjectionRunner,
) -> list[tuple[str, bytes]]:
    """Capture the SNAPSHOT leg, which uses the runner's own producer rather
    than the ``publish_fn`` terminal-envelope seam."""
    sent: list[tuple[str, bytes]] = []

    async def _send_and_wait(topic: str, **kwargs: Any) -> None:
        sent.append((topic, kwargs["value"]))

    producer = AsyncMock()
    producer.send_and_wait = AsyncMock(side_effect=_send_and_wait)
    runner._ensure_producer = AsyncMock(return_value=producer)  # type: ignore[method-assign]
    return sent


@asynccontextmanager
async def _runner_on_schema(
    *, exclude_migration: str | None = None
) -> AsyncIterator[tuple[DelegationProjectionRunner, asyncpg.Connection, str]]:
    """The live migration chain on a disposable schema, optionally minus one file."""
    admin_conn = await _connect_or_skip()
    schema = f"omn18140_{uuid4().hex[:16]}"
    pool: asyncpg.Pool | None = None
    try:
        await admin_conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await admin_conn.execute(f"CREATE SCHEMA {schema}")
        await admin_conn.execute(f"SET search_path TO {schema}, public")
        await admin_conn.execute(_APP_DASHBOARD_ROLE_SQL)
        for migration_path in _live_migration_files(exclude=exclude_migration):
            await admin_conn.execute(
                _test_schema_safe_sql(migration_path.read_text(encoding="utf-8"))
            )
        await admin_conn.execute(_TENANT_REGISTRY_MIRROR_SQL)
        # The writer resolves its tenant through the registry mirror and
        # refuses an identity nobody recorded, so the fixture must hold the row
        # the deployed lane holds -- otherwise this module's subject silently
        # becomes tenant resolution, which has its own tests.
        await admin_conn.execute(
            "INSERT INTO tenant_registry_mirror "
            "(tenant_slug, tenant_uuid, status) VALUES ($1, $2::uuid, 'active') "
            "ON CONFLICT (tenant_slug) DO NOTHING",
            _TENANT_SLUG,
            _TENANT_UUID,
        )
        pool = await asyncpg.create_pool(
            _base_dsn(),
            min_size=1,
            max_size=3,
            server_settings={"search_path": f"{schema},public"},
        )
        adapter = AsyncpgAdapter(dsn=_base_dsn())
        adapter._pool = pool  # type: ignore[attr-defined]
        runner = DelegationProjectionRunner(publish_fn=_capture)
        runner._db = adapter  # type: ignore[assignment]
        yield runner, admin_conn, schema
    finally:
        if pool is not None:
            with contextlib.suppress(Exception):
                await pool.close()
        with contextlib.suppress(Exception):
            await admin_conn.execute("SET search_path TO public")
            await admin_conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await admin_conn.close()


def _task_delegated_delivery(*, correlation_id: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "correlation_id": correlation_id,
        "task_type": "code_review",
        "delegated_to": "local",
        "model_name": "qwen2.5-coder",
        # Deliberately a lie about the writer. `delegated_by` is caller-supplied
        # and this module asserts it never reaches `writer_identity`.
        "delegated_by": "an-application-actor-not-a-database-principal",
        "quality_gate_passed": True,
        "cost_usd": 0.0,
        "cost_savings_usd": 0.12,
        "tokens_input": 100,
        "tokens_output": 50,
        "timestamp": _ENVELOPE_TIMESTAMP.isoformat(),
    }
    envelope = {
        "payload": payload,
        "envelope_id": str(uuid4()),
        "correlation_id": correlation_id,
        "event_type": "omnibase-infra.task-delegated",
        "envelope_timestamp": _ENVELOPE_TIMESTAMP.isoformat(),
        "tenant_id": _TENANT_UUID,
    }
    unwrapped = unwrap_envelope(json.dumps(envelope).encode("utf-8"))
    assert unwrapped is not None
    return unwrapped


async def _project(
    runner: DelegationProjectionRunner, *, correlation_id: str, offset: int = 1
) -> bool:
    return await runner.project_event(
        TASK_DELEGATED_TOPIC_V1,
        _task_delegated_delivery(correlation_id=correlation_id),
        MessageMeta(partition=0, offset=offset, fallback_id=correlation_id, topic="t"),
    )


async def _column_names(conn: asyncpg.Connection, schema: str) -> Sequence[str]:
    rows = await conn.fetch(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = $1 AND table_name = 'delegation_events'",
        schema,
    )
    return [str(row["column_name"]) for row in rows]


# ---------------------------------------------------------------------------
# Positive control, then RED.
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestTheFixtureIsFaithful:
    """If the excluded-migration schema is not actually missing the column,
    the RED half below passes for the wrong reason."""

    def test_the_parent_schema_lacks_both_columns(self) -> None:
        async def _run() -> None:
            async with _runner_on_schema(exclude_migration=_MIGRATION_UNDER_TEST) as (
                _,
                conn,
                schema,
            ):
                columns = await _column_names(conn, schema)
                assert columns, "the fixture built no delegation_events table"
                for column in _ATTESTATION_COLUMNS:
                    assert column not in columns, column

        asyncio.run(_run())

    def test_the_migrated_schema_has_both_columns(self) -> None:
        async def _run() -> None:
            async with _runner_on_schema() as (_, conn, schema):
                columns = await _column_names(conn, schema)
                for column in _ATTESTATION_COLUMNS:
                    assert column in columns, column

        asyncio.run(_run())


@pytest.mark.integration
class TestTheSchemaWithoutTheMigrationRefusesTheWrite:
    """RED. The live writer against the parent commit's schema."""

    def test_the_write_fails_naming_the_missing_column(self) -> None:
        async def _run() -> None:
            async with _runner_on_schema(exclude_migration=_MIGRATION_UNDER_TEST) as (
                runner,
                _,
                _schema,
            ):
                with pytest.raises(asyncpg.exceptions.UndefinedColumnError) as exc:
                    await _project(runner, correlation_id=str(uuid4()))
                assert "writer_identity" in str(exc.value)

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# GREEN.
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestTheWrittenRowAttestsToItsWriter:
    def test_the_stamp_is_the_connections_own_principal(self) -> None:
        """Not a value this process chose: the assertion compares against what
        the DATABASE reports for the same session."""

        async def _run() -> None:
            async with _runner_on_schema() as (runner, conn, _schema):
                correlation_id = str(uuid4())
                assert await _project(runner, correlation_id=correlation_id)
                row = await conn.fetchrow(
                    "SELECT writer_identity, written_at, delegated_by "
                    "FROM delegation_events WHERE correlation_id = $1",
                    correlation_id,
                )
                assert row is not None
                expected = await conn.fetchval("SELECT CURRENT_USER")
                assert row["writer_identity"] == expected
                assert row["written_at"] is not None

        asyncio.run(_run())

    def test_the_caller_supplied_delegator_never_reaches_the_writer_column(
        self,
    ) -> None:
        """``delegated_by`` is the delegator and is attacker-controlled in the
        sense that matters: it is whatever the inbound event said."""

        async def _run() -> None:
            async with _runner_on_schema() as (runner, conn, _schema):
                correlation_id = str(uuid4())
                assert await _project(runner, correlation_id=correlation_id)
                row = await conn.fetchrow(
                    "SELECT writer_identity, delegated_by FROM delegation_events "
                    "WHERE correlation_id = $1",
                    correlation_id,
                )
                assert row is not None
                assert (
                    row["delegated_by"]
                    == "an-application-actor-not-a-database-principal"
                )
                assert row["writer_identity"] != row["delegated_by"]

        asyncio.run(_run())

    def test_the_update_arm_refreshes_the_stamp(self) -> None:
        """A column DEFAULT is consulted only on INSERT. Without the expression
        on the DO UPDATE arm a row would wear its first writer's identity and
        its first write's timestamp for the rest of its life -- and the
        readback orders on exactly that timestamp."""

        async def _run() -> None:
            async with _runner_on_schema() as (runner, conn, _schema):
                correlation_id = str(uuid4())
                assert await _project(runner, correlation_id=correlation_id, offset=1)
                first = await conn.fetchval(
                    "SELECT written_at FROM delegation_events "
                    "WHERE correlation_id = $1",
                    correlation_id,
                )
                await asyncio.sleep(0.05)
                assert await _project(runner, correlation_id=correlation_id, offset=2)
                second = await conn.fetchval(
                    "SELECT written_at FROM delegation_events "
                    "WHERE correlation_id = $1",
                    correlation_id,
                )
                assert second > first

        asyncio.run(_run())

    def test_the_upsert_still_holds_one_row_per_correlation(self) -> None:
        """The attestation rides the same upsert; it does not fork the key."""

        async def _run() -> None:
            async with _runner_on_schema() as (runner, conn, _schema):
                correlation_id = str(uuid4())
                assert await _project(runner, correlation_id=correlation_id, offset=1)
                assert await _project(runner, correlation_id=correlation_id, offset=2)
                count = await conn.fetchval(
                    "SELECT count(*) FROM delegation_events WHERE correlation_id = $1",
                    correlation_id,
                )
                assert count == 1

        asyncio.run(_run())


@pytest.mark.integration
class TestTheRepublishedRowCarriesWhatTheReadbackReads:
    """The end of the chain: what a reader of the exposure would see.

    The leg-4 readback reads ``tenant_id``, ``writer_identity`` and
    ``written_at`` off each served row, and the projection API serves whatever
    the writer published -- it holds no database handle. So the published delta
    is the last place these three can go missing, and it is asserted against a
    row that came out of a real database.
    """

    def test_the_delta_carries_all_three_readback_keys(self) -> None:
        async def _run() -> None:
            async with _runner_on_schema() as (runner, _conn, _schema):
                sent = _intercept_snapshot_sends(runner)
                correlation_id = str(uuid4())
                assert await _project(runner, correlation_id=correlation_id)
                topic = _row_exposure().topic
                deltas = [value for sent_topic, value in sent if sent_topic == topic]
                assert deltas, f"nothing was republished onto {topic!r}"
                row = json.loads(deltas[-1])["row"]
                for key in ("tenant_id", "writer_identity", "written_at"):
                    assert row.get(key) is not None, key

        asyncio.run(_run())

    def test_the_delta_is_keyed_on_the_tables_conflict_key(self) -> None:
        async def _run() -> None:
            async with _runner_on_schema() as (runner, _conn, _schema):
                sent = _intercept_snapshot_sends(runner)
                correlation_id = str(uuid4())
                assert await _project(runner, correlation_id=correlation_id)
                topic = _row_exposure().topic
                deltas = [value for sent_topic, value in sent if sent_topic == topic]
                assert deltas
                assert json.loads(deltas[-1])["key"] == [correlation_id]

        asyncio.run(_run())
