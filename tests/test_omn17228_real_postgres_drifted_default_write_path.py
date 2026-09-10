# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17228: real-Postgres proof that the quality-gate verdict writes on a lane
whose ``delegation_events`` DEFAULTs went missing.

WHY A REAL DATABASE IS REQUIRED HERE, and why the mock-DB module
``tests/test_omn17228_quality_gate_result_not_null_columns.py`` is not enough.
That module asserts the shape of the statement the writer PROPOSES -- which
columns it names, and which it keeps out of ``DO UPDATE SET``. It cannot
observe the thing that actually broke: Postgres evaluates NOT NULL against the
proposed INSERT row BEFORE the conflict is resolved, so a targeted-column
UPSERT that omits a no-default NOT NULL column is refused outright even when it
was only ever going to take the DO UPDATE arm. An ``AsyncMock`` accepts any
row dict, so every mock-DB test in this repo passed while the live writer
dead-lettered 28 verdicts. Only a real connection enforces the constraint.

WHAT MAKES THIS LANE DIFFERENT, MEASURED. ``0007_delegation_events.sql``
declares ``task_type TEXT NOT NULL DEFAULT ''`` and ``delegated_to TEXT NOT
NULL DEFAULT ''``, so a schema built by applying the migrations to an empty
database HAS those defaults, and the defect is unreachable on it. Two live
readbacks, same day, prove the split:

* ``.201`` compose dev lane (``omnibase-infra-postgres``, fresh-create path),
  2026-09-10 -- ``task_type ''::text``, ``delegated_to ''::text``,
  ``timestamp now()``. Defaults present. The defect CANNOT be reproduced there.
* onex-dev RDS ``omnidash_analytics`` (DEV-SYSTEM ``i-06169517a92b45f86``),
  2026-09-10 -- ``task_type`` and ``delegated_to`` ``is_nullable=NO`` with
  ``column_default=NULL``; ``timestamp`` likewise. Defaults absent.

The mechanism is ``0007``'s own OMN-15376 reconciliation block: it re-declares
each column as ``ADD COLUMN IF NOT EXISTS ... DEFAULT ''``, which no-ops on a
column that already exists and therefore never installs the missing DEFAULT on
a table that predates the migration. ``model_name`` -- declared identically --
did acquire its default on onex-dev, because that column did not pre-exist.
Nothing in the migration distinguishes the two cases.

So this module builds the migrated schema and then DROPS those defaults, which
is the only faithful way to reproduce the deployed lane in a test. The drop is
not a convenience: without it the RED half cannot fail, and a test that cannot
fail proves nothing. ``TestTheDriftedFixtureIsFaithful`` asserts the fixture
really is in the drifted state before any behaviour is asserted on it.

Harness (``_connect_or_skip`` / disposable schema / guarded ``app_dashboard``
role) mirrors ``tests/test_omn15909_real_postgres_projection_write_path_gate.py``
byte-for-byte in spirit: SKIPS rather than ERRORs without a reachable database,
and provisions a throwaway schema so runs never collide.
"""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote_plus
from uuid import uuid4

import asyncpg
import pytest

from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter
from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation import (
    DelegationProjectionRunner,
)
from omnimarket.projection.envelope import unwrap_envelope
from omnimarket.projection.runner import MessageMeta

_MIGRATIONS_DIR = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_delegation"
    / "migrations"
)

#: The onex-dev drift, reproduced. These are the columns measured there as
#: NOT NULL with ``column_default = NULL`` that a fresh migrated schema gives a
#: default to.
_COLUMNS_WITH_DROPPED_DEFAULT = ("task_type", "delegated_to", "timestamp")

_ENVELOPE_TIMESTAMP = datetime(2026, 9, 10, 5, 1, 33, 932000, tzinfo=UTC)
_TENANT_UUID = "820272f9-4aaf-5add-a2df-0af942852ab2"
_TENANT_SLUG = "omninode"

#: ``tenant_registry_mirror`` lives under a DIFFERENT node
#: (``node_projection_tenant_registry``), so this module's migration glob does
#: not create it -- but the delegation writer reads it on every write to
#: resolve its tenant. Created here from that node's own DDL rather than
#: restated, so the fixture cannot drift from the table the writer queries.
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
ALTER ROLE app_dashboard
  NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION;
"""


def _live_migration_files() -> list[Path]:
    return sorted(_MIGRATIONS_DIR.glob("*.sql"))


def _test_schema_safe_sql(raw_sql: str) -> str:
    """Strip ``CONCURRENTLY`` -- it refuses to run inside the implicit
    transaction asyncpg's multi-statement ``execute()`` opens. It exists only
    to avoid locking a live table, which a disposable schema does not have."""
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
            "POSTGRES_PASSWORD not set -- skipping OMN-17228 drifted-default "
            "write-path gate"
        )
    try:
        return await asyncpg.connect(_base_dsn())
    except (OSError, asyncpg.PostgresError) as exc:  # pragma: no cover - infra
        pytest.skip(f"no reachable Postgres for OMN-17228 write-path gate: {exc}")


async def _capture(topic: str, value: bytes) -> None:
    """The runner's real ``publish_fn`` seam -- without one, ``get_publish_fn``
    builds a real ``AIOKafkaProducer`` and blocks on connect retries."""
    return


@asynccontextmanager
async def _drifted_runner() -> AsyncIterator[
    tuple[DelegationProjectionRunner, asyncpg.Connection]
]:
    """The live migrated schema, then the onex-dev drift applied on top."""
    admin_conn = await _connect_or_skip()
    schema = f"omn17228_{uuid4().hex[:16]}"
    pool: asyncpg.Pool | None = None
    try:
        await admin_conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await admin_conn.execute(f"CREATE SCHEMA {schema}")
        await admin_conn.execute(f"SET search_path TO {schema}, public")
        await admin_conn.execute(_APP_DASHBOARD_ROLE_SQL)
        for migration_path in _live_migration_files():
            await admin_conn.execute(
                _test_schema_safe_sql(migration_path.read_text(encoding="utf-8"))
            )
        await admin_conn.execute(_TENANT_REGISTRY_MIRROR_SQL)
        # The writer resolves its write tenant through the registry mirror and
        # refuses an identity nobody recorded (OMN-16804/OMN-16831), so the
        # fixture must hold the row the deployed lane holds. Seeding it is what
        # keeps this module's subject the NOT NULL defect rather than tenant
        # resolution, which has its own tests.
        await admin_conn.execute(
            "INSERT INTO tenant_registry_mirror "
            "(tenant_slug, tenant_uuid, status) VALUES ($1, $2::uuid, 'active') "
            "ON CONFLICT (tenant_slug) DO NOTHING",
            _TENANT_SLUG,
            _TENANT_UUID,
        )

        # Reproduce the deployed lane. See the module docstring: a fresh
        # migrated schema carries these defaults, onex-dev does not, and the
        # difference is invisible from the migration text alone.
        for column in _COLUMNS_WITH_DROPPED_DEFAULT:
            await admin_conn.execute(
                f"ALTER TABLE delegation_events ALTER COLUMN {column} DROP DEFAULT"
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
        yield runner, admin_conn
    finally:
        if pool is not None:
            with contextlib.suppress(Exception):
                await pool.close()
        with contextlib.suppress(Exception):
            await admin_conn.execute("SET search_path TO public")
            await admin_conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await admin_conn.close()


def _quality_gate_delivery(*, correlation_id: str) -> dict[str, object]:
    payload: dict[str, object] = {
        "correlation_id": correlation_id,
        "passed": True,
        "fail_category": "pass",
        "quality_score": 1.0,
        "failure_reasons": [],
        "fallback_recommended": False,
        "score_source": "deterministic_acceptance",
        "actual_score": 1.0,
    }
    envelope = {
        "payload": payload,
        "envelope_id": str(uuid4()),
        "correlation_id": correlation_id,
        "event_type": "omnibase-infra.quality-gate-result",
        "envelope_timestamp": _ENVELOPE_TIMESTAMP.isoformat(),
        "tenant_id": _TENANT_UUID,
    }
    unwrapped = unwrap_envelope(json.dumps(envelope).encode("utf-8"))
    assert unwrapped is not None
    return unwrapped


@pytest.mark.integration
class TestTheDriftedFixtureIsFaithful:
    """Positive control for the whole module. If the fixture is not actually in
    the drifted state, every behavioural assertion below passes for the wrong
    reason."""

    def test_the_defaults_really_are_gone(self) -> None:
        import asyncio

        async def _run() -> None:
            async with _drifted_runner() as (_runner, admin_conn):
                rows = await admin_conn.fetch(
                    "SELECT column_name, is_nullable, column_default "
                    "FROM information_schema.columns "
                    "WHERE table_name = 'delegation_events' "
                    "AND column_name = ANY($1::text[])",
                    list(_COLUMNS_WITH_DROPPED_DEFAULT),
                )
                by_column = {r["column_name"]: r for r in rows}
                assert set(by_column) == set(_COLUMNS_WITH_DROPPED_DEFAULT)
                for column in _COLUMNS_WITH_DROPPED_DEFAULT:
                    assert by_column[column]["is_nullable"] == "NO", column
                    assert by_column[column]["column_default"] is None, (
                        f"{column} still has a DEFAULT -- the fixture does not "
                        "reproduce onex-dev and the RED half below cannot fail"
                    )

        asyncio.run(_run())

    def test_an_omitting_insert_is_refused_by_real_postgres(self) -> None:
        """The RED proof, isolated from the writer: on this schema an INSERT
        that omits ``task_type`` raises 23502. This is the exact error the
        deployed writer produced 28 times, and it is what the fix must avoid
        by naming the column."""
        import asyncio

        async def _run() -> None:
            async with _drifted_runner() as (_runner, admin_conn):
                with pytest.raises(asyncpg.exceptions.NotNullViolationError) as exc:
                    await admin_conn.execute(
                        "INSERT INTO delegation_events "
                        "(correlation_id, timestamp, delegated_to, tenant_id) "
                        "VALUES ($1, $2, '', $3::uuid)",
                        str(uuid4()),
                        _ENVELOPE_TIMESTAMP,
                        _TENANT_UUID,
                    )
                assert "task_type" in str(exc.value)

        asyncio.run(_run())


@pytest.mark.integration
class TestTheVerdictLandsOnTheDriftedLane:
    def test_quality_gate_verdict_writes_a_row(self) -> None:
        """RED before OMN-17228: this raised
        ``null value in column "task_type" of relation "delegation_events"
        violates not-null constraint`` and the verdict went to the DLQ with its
        offset committed."""
        import asyncio

        async def _run() -> None:
            async with _drifted_runner() as (runner, admin_conn):
                correlation_id = str(uuid4())

                ok = await runner.project_event(
                    runner._topic_quality_gate_result,
                    _quality_gate_delivery(correlation_id=correlation_id),
                    MessageMeta(partition=0, offset=208, fallback_id=correlation_id),
                )
                assert ok is True

                row = await admin_conn.fetchrow(
                    "SELECT task_type, delegated_to, timestamp, "
                    "quality_gate_passed, actual_score "
                    "FROM delegation_events WHERE correlation_id = $1",
                    correlation_id,
                )
                assert row is not None, (
                    "the verdict must land a row on a lane whose DEFAULTs are "
                    "missing -- this is the whole defect"
                )
                assert row["task_type"] == ""
                assert row["delegated_to"] == ""
                assert row["timestamp"] == _ENVELOPE_TIMESTAMP
                assert row["quality_gate_passed"] is True

        asyncio.run(_run())

    def test_a_later_terminal_overwrites_the_placeholders(self) -> None:
        """The self-healing half, proven against real Postgres rather than
        argued in a comment. In the measured corpus the verdict arrives BEFORE
        its terminal every time, so the placeholder row is the common case and
        the terminal must be able to correct it."""
        import asyncio

        async def _run() -> None:
            async with _drifted_runner() as (runner, admin_conn):
                correlation_id = str(uuid4())

                assert (
                    await runner.project_event(
                        runner._topic_quality_gate_result,
                        _quality_gate_delivery(correlation_id=correlation_id),
                        MessageMeta(
                            partition=0, offset=208, fallback_id=correlation_id
                        ),
                    )
                    is True
                )

                # The terminal event names task_type/delegated_to and holds
                # neither insert-only, so its DO UPDATE arm corrects them.
                await admin_conn.execute(
                    "INSERT INTO delegation_events "
                    "(correlation_id, timestamp, task_type, delegated_to, tenant_id) "
                    "VALUES ($1, $2, 'code-review', 'glm-5.2', $3::uuid) "
                    "ON CONFLICT (correlation_id) DO UPDATE SET "
                    "task_type = EXCLUDED.task_type, "
                    "delegated_to = EXCLUDED.delegated_to",
                    correlation_id,
                    _ENVELOPE_TIMESTAMP,
                    _TENANT_UUID,
                )

                row = await admin_conn.fetchrow(
                    "SELECT task_type, delegated_to FROM delegation_events "
                    "WHERE correlation_id = $1",
                    correlation_id,
                )
                assert row is not None
                assert row["task_type"] == "code-review"
                assert row["delegated_to"] == "glm-5.2"

        asyncio.run(_run())

    def test_a_later_verdict_never_erases_a_recorded_task_type(self) -> None:
        """The reverse direction, which is why both columns are insert-only.
        A verdict arriving after its terminal must not overwrite a real task
        type with the empty-string placeholder."""
        import asyncio

        async def _run() -> None:
            async with _drifted_runner() as (runner, admin_conn):
                correlation_id = str(uuid4())

                await admin_conn.execute(
                    "INSERT INTO delegation_events "
                    "(correlation_id, timestamp, task_type, delegated_to, tenant_id) "
                    "VALUES ($1, $2, 'code-review', 'glm-5.2', $3::uuid)",
                    correlation_id,
                    _ENVELOPE_TIMESTAMP,
                    _TENANT_UUID,
                )

                assert (
                    await runner.project_event(
                        runner._topic_quality_gate_result,
                        _quality_gate_delivery(correlation_id=correlation_id),
                        MessageMeta(
                            partition=0, offset=258, fallback_id=correlation_id
                        ),
                    )
                    is True
                )

                row = await admin_conn.fetchrow(
                    "SELECT task_type, delegated_to, quality_gate_passed "
                    "FROM delegation_events WHERE correlation_id = $1",
                    correlation_id,
                )
                assert row is not None
                assert row["task_type"] == "code-review", (
                    "the verdict's empty-string placeholder must never replace "
                    "a task type a terminal event already recorded"
                )
                assert row["delegated_to"] == "glm-5.2"
                assert row["quality_gate_passed"] is True, (
                    "positive control: the verdict must still have updated the "
                    "columns it does own, or the assertions above hold for the "
                    "trivial reason that nothing was written at all"
                )

        asyncio.run(_run())
