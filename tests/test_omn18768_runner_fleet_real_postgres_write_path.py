# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18768: real-Postgres write-path proof for the runner-fleet projection.

Why this is NOT redundant with ``tests/test_omn18768_runner_fleet_writer.py``:
that module drives the same entrypoint against a recording DB double, which
accepts a bound parameter of ANY Python type — an ``.isoformat()`` string binds
just as "successfully" as a real ``datetime``, and a Python list binds just as
"successfully" as JSON. Only a real Postgres connection enforces column types
through asyncpg's extended query protocol. That gap is exactly how the
OMN-15905 str-where-datetime defect reached a merged, deployed,
CrashLoopBackOff-ing runtime with every layer of mock-DB coverage passing.

This projection has two type hazards a mock cannot see, and one behaviour that
is implemented IN SQL and therefore has no Python to test:

  * ``observed_at`` is TIMESTAMPTZ and arrives off the wire as an ISO string
    that pydantic parses to a ``datetime``. A regression that passed the raw
    string through would bind fine against a mock.
  * ``labels`` is JSONB. A Python list bound directly is rejected by asyncpg;
    only the real driver says so.
  * The stale-write guard is the ``ON CONFLICT ... WHERE observed_at <
    EXCLUDED.observed_at`` clause. There is no Python branch to unit test —
    the database is the whole mechanism, so this is the only place it can be
    proven.

Real Postgres, never SQLite: SQLite's loosely-affinity-typed columns accept a
``str`` into a TIMESTAMP column without complaint, which is the exact hole this
gate closes. The harness mirrors ``tests/test_writer_tenant_isolation_omn14898.py``
— it SKIPS (never ERRORs) without a reachable database and provisions its own
throwaway schema so runs never collide.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote_plus

import asyncpg
import pytest

_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_runner_fleet"
    / "migrations"
    / "0000_create_runner_fleet_liveness.sql"
)

_T0 = datetime(2026, 9, 18, 23, 46, 31, tzinfo=UTC)
_SCHEMA = "omn18768_runner_fleet_write_path_test"

# The writer's SQL, schema-qualified at import time in the module under test.
# Read from the module rather than restated, so a change to the real statement
# is proven here instead of drifting away from a copy.
from omnimarket.nodes.node_projection_runner_fleet.handlers.handler_fleet_liveness_writer import (  # noqa: E402
    _DELETE_RUNNER,
    _SELECT_SUPERSEDED,
    _UPSERT_RUNNER,
)


async def _connect_or_skip() -> asyncpg.Connection:
    password = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    if not password:
        pytest.skip(
            "INTEGRATION_POSTGRES_PASSWORD not set -- skipping runner-fleet write-path DB proof"
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
        pytest.skip(f"no reachable Postgres for runner-fleet write-path proof: {exc}")


def _scoped(statement: str, schema: str) -> str:
    """A real statement, retargeted at the disposable fixture schema."""
    return statement.replace("omninode_internal.", f"{schema}.")


def _scoped_upsert(schema: str) -> str:
    """The real upsert, retargeted at the disposable fixture schema."""
    return _scoped(_UPSERT_RUNNER, schema)


async def _setup(conn: asyncpg.Connection) -> None:
    await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
    await conn.execute(f"CREATE SCHEMA {_SCHEMA}")
    sql = _MIGRATION.read_text(encoding="utf-8").replace(
        "omninode_internal.", f"{_SCHEMA}."
    )
    await conn.execute(sql)


async def _upsert(
    conn: asyncpg.Connection,
    *,
    name: str,
    status: str,
    observed_at: datetime,
    host: str = "host-105",
    label_class: str = "omnibase-verify",
) -> list[asyncpg.Record]:
    return await conn.fetch(
        _scoped_upsert(_SCHEMA),
        name,
        141087,
        label_class,
        json.dumps(["self-hosted", "Linux", "ARM64", label_class, host]),
        host,
        "omni-201",
        status,
        None,
        observed_at,
    )


@pytest.mark.integration
async def test_real_postgres_accepts_the_writers_own_bound_types() -> None:
    """The live row lands with correctly-typed columns, via the real statement."""
    conn = await _connect_or_skip()
    try:
        await _setup(conn)
        rows = await _upsert(
            conn, name="omninode-air-runner-1", status="offline", observed_at=_T0
        )
        assert len(rows) == 1
        row = rows[0]
        assert row["runner_name"] == "omninode-air-runner-1"
        assert row["status"] == "offline"
        assert row["host"] == "host-105"
        assert row["observing_host"] == "omni-201"
        # TIMESTAMPTZ, not a string. A mock DB cannot tell these apart.
        assert isinstance(row["observed_at"], datetime)
        assert row["observed_at"] == _T0
        # JSONB round-trips as a JSON document, not a Python repr.
        assert json.loads(row["labels"])[0] == "self-hosted"
        # The database assigns the page cursor; nothing in Python supplies it.
        assert isinstance(row["projection_cursor"], int)
    finally:
        await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        await conn.close()


@pytest.mark.integration
async def test_real_postgres_refuses_a_stale_observation() -> None:
    """The stale-write guard is SQL, so this is the only place it can be proven.

    A redelivered observation older than what is stored must return NO row, so
    the writer knows not to republish it — pushing an older row at the
    bus-backed cache would undo a newer one the database itself declined.
    """
    conn = await _connect_or_skip()
    try:
        await _setup(conn)
        newer = _T0 + timedelta(minutes=3)
        await _upsert(
            conn, name="omninode-runner-1", status="online", observed_at=newer
        )

        refused = await _upsert(
            conn, name="omninode-runner-1", status="offline", observed_at=_T0
        )
        assert refused == [], (
            "a stale observation must be refused by the ON CONFLICT guard"
        )

        stored = await conn.fetchrow(
            f"SELECT status, observed_at FROM {_SCHEMA}.runner_fleet_liveness "
            "WHERE runner_name = $1",
            "omninode-runner-1",
        )
        assert stored["status"] == "online"
        assert stored["observed_at"] == newer

        # Positive control: a NEWER observation is accepted, so the refusal
        # above is the guard working rather than the statement never writing.
        accepted = await _upsert(
            conn,
            name="omninode-runner-1",
            status="offline",
            observed_at=newer + timedelta(minutes=3),
        )
        assert len(accepted) == 1
        assert accepted[0]["status"] == "offline"
    finally:
        await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        await conn.close()


@pytest.mark.integration
async def test_real_postgres_refuses_a_status_the_schema_does_not_know() -> None:
    """The CHECK constraint is the last line against an emitter rename.

    Without it, an emitter that started writing 'Online' would split the fleet
    in two on every panel that groups by status, silently.
    """
    conn = await _connect_or_skip()
    try:
        await _setup(conn)
        with pytest.raises(asyncpg.CheckViolationError):
            await _upsert(
                conn, name="omninode-runner-1", status="Online", observed_at=_T0
            )
    finally:
        await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        await conn.close()


@pytest.mark.integration
async def test_real_postgres_scopes_the_tombstone_on_supersession_and_observer() -> (
    None
):
    """The tombstone scope is SQL, so a real database is the only place it holds.

    The adversarial reviewer blocked this node once on EACH horn of the scope
    choice, and both findings were correct about the shape they saw: the
    host-only scope let a deregistered runner linger reporting ``online``
    (row ownership moves on every upsert beside a single-column primary key),
    and the supersession-only scope let a narrower observer delete runners it
    never observed. Only the conjunction answers both.

    Neither predicate has a Python branch, so a mock-DB test cannot see either:

      * the lookup returns only the rows this observation supersedes AND this
        observer owns
      * the delete removes only such a row, so a peer's fresher row and a
        peer's own rows both survive it
    """
    conn = await _connect_or_skip()
    try:
        await _setup(conn)
        older = _T0
        newer = _T0 + timedelta(minutes=3)
        peer = "omni-105"

        # An earlier cycle of THIS observer, a row a peer observer just wrote,
        # and an older row belonging to the peer.
        await _upsert(
            conn, name="omninode-runner-1", status="online", observed_at=older
        )
        await _upsert(
            conn, name="omninode-runner-2", status="online", observed_at=newer
        )
        await conn.execute(
            f"UPDATE {_SCHEMA}.runner_fleet_liveness SET observing_host = $1 "
            "WHERE runner_name = $2",
            peer,
            "omninode-runner-2",
        )
        await _upsert(
            conn, name="omninode-runner-3", status="online", observed_at=older
        )
        await conn.execute(
            f"UPDATE {_SCHEMA}.runner_fleet_liveness SET observing_host = $1 "
            "WHERE runner_name = $2",
            peer,
            "omninode-runner-3",
        )

        superseded = [
            record["runner_name"]
            for record in await conn.fetch(
                _scoped(_SELECT_SUPERSEDED, _SCHEMA), newer, "omni-201"
            )
        ]
        assert superseded == ["omninode-runner-1"], (
            "the lookup must return only rows this observation supersedes AND "
            f"this observer owns; got {superseded!r}"
        )

        # A peer's OLDER row is superseded but is not this observer's to delete.
        await conn.execute(
            _scoped(_DELETE_RUNNER, _SCHEMA), "omninode-runner-3", newer, "omni-201"
        )
        # A peer's row at this observation's own timestamp is not superseded.
        await conn.execute(
            _scoped(_DELETE_RUNNER, _SCHEMA), "omninode-runner-2", newer, peer
        )
        surviving = sorted(
            record["runner_name"]
            for record in await conn.fetch(
                f"SELECT runner_name FROM {_SCHEMA}.runner_fleet_liveness"
            )
        )
        assert surviving == [
            "omninode-runner-1",
            "omninode-runner-2",
            "omninode-runner-3",
        ], f"a row that neither predicate admits was deleted: {surviving!r}"

        # The row this observation supersedes AND owns IS removed.
        await conn.execute(
            _scoped(_DELETE_RUNNER, _SCHEMA), "omninode-runner-1", newer, "omni-201"
        )
        remaining = sorted(
            record["runner_name"]
            for record in await conn.fetch(
                f"SELECT runner_name FROM {_SCHEMA}.runner_fleet_liveness"
            )
        )
        assert remaining == ["omninode-runner-2", "omninode-runner-3"], remaining

        # Both predicates present in both statements, asserted on the real
        # statements this module imports rather than on a copy.
        for statement in (_SELECT_SUPERSEDED, _DELETE_RUNNER):
            assert "observed_at <" in statement
            assert "observing_host" in statement
    finally:
        await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        await conn.close()
