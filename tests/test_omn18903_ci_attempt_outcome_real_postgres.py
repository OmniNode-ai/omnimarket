# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18903 AC-6: real-Postgres write-path proof for the attempt-outcome rows.

Why this is not redundant with the golden-chain module: that one drives the
same entry against a recording double, which accepts a bound parameter of ANY
Python type. An ISO string binds just as "successfully" as a real datetime.
Only a real connection enforces column types through the driver's extended
query protocol, and that gap is how the OMN-15905 string-where-datetime defect
reached a merged, deployed, crash-looping runtime with every layer of mock
coverage passing.

Three things here have no Python to test and can only be proven against a real
server:

  * ``observed_at`` is a timestamp with time zone and arrives off the wire as
    an ISO string that the model parses. A regression passing the raw string
    through would bind fine against a double.
  * The stale-write guard is the conflict arm's WHERE clause. There is no
    Python branch -- the database is the whole mechanism.
  * The six-value check constraint on the cause column. A seventh enum member
    added without widening the migration is refused HERE and nowhere else.

Real Postgres, never SQLite: SQLite's loose column affinity accepts a string
into a timestamp column without complaint, which is the exact hole this closes.
The harness SKIPS rather than ERRORs without a reachable database, and
provisions its own throwaway schema so runs never collide.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote_plus

import asyncpg
import pytest

from omnimarket.merge_control.reason_code_classifier import EnumMergeCheckReasonCode

# The writer's real statement, read from the module rather than restated, so a
# change to it is proven here instead of drifting away from a copy.
from omnimarket.nodes.node_projection_ci_attempt_outcome.handlers.handler_ci_attempt_outcome_writer import (
    _UPSERT_ATTEMPT,
)

_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_ci_attempt_outcome"
    / "migrations"
    / "0000_create_ci_attempt_outcome.sql"
)

_T0 = datetime(2026, 9, 20, 12, 0, 0, tzinfo=UTC)
_SCHEMA = "omn18903_ci_attempt_outcome_write_path_test"
_SHA = "a" * 40
_REPO = "OmniNode-ai/omnimarket"
_PR = 2726


async def _connect_or_skip() -> asyncpg.Connection:
    secret = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    if not secret:
        pytest.skip(
            "INTEGRATION_POSTGRES_PASSWORD not set -- skipping the "
            "attempt-outcome write-path proof"
        )
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = int(os.environ.get("INTEGRATION_POSTGRES_PORT", "5432"))
    user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
    db = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
    dsn = f"postgresql://{quote_plus(user)}:{quote_plus(secret)}@{host}:{port}/{db}"
    try:
        return await asyncpg.connect(dsn)
    except (OSError, asyncpg.PostgresError) as exc:  # pragma: no cover
        pytest.skip(f"no reachable Postgres for the write-path proof: {exc}")


def _scoped(statement: str) -> str:
    """A real statement, retargeted at the disposable fixture schema."""
    return statement.replace("omninode_internal.", f"{_SCHEMA}.")


async def _setup(conn: asyncpg.Connection) -> None:
    await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
    await conn.execute(f"CREATE SCHEMA {_SCHEMA}")
    await conn.execute(_scoped(_MIGRATION.read_text(encoding="utf-8")))


async def _upsert(
    conn: asyncpg.Connection,
    *,
    observed_at: datetime,
    cause_code: str = "process_gate_refused",
    check_name: str = "verify",
    run_attempt: int = 1,
    ticket_id: str | None = "OMN-18903",
) -> list[asyncpg.Record]:
    return await conn.fetch(
        _scoped(_UPSERT_ATTEMPT),
        _REPO,
        _PR,
        _SHA,
        check_name,
        run_attempt,
        cause_code,
        True,
        1,
        ticket_id,
        "Resolve Evidence-Source",
        "5501",
        "failure",
        observed_at,
    )


@pytest.mark.integration
async def test_the_row_reads_back_with_typed_column_values() -> None:
    """The live row lands with correctly-typed columns, via the real statement."""
    conn = await _connect_or_skip()
    try:
        await _setup(conn)
        returned = await _upsert(conn, observed_at=_T0)
        assert len(returned) == 1

        row = await conn.fetchrow(f"SELECT * FROM {_SCHEMA}.ci_attempt_outcome")
        assert row is not None
        # The types, which are the point: a double would have accepted
        # strings for all three of these.
        assert isinstance(row["observed_at"], datetime)
        assert row["observed_at"] == _T0
        assert isinstance(row["cause_affirmative"], bool)
        assert isinstance(row["attempt_ordinal"], int)
        assert row["cause_code"] == "process_gate_refused"
        assert row["ticket_id"] == "OMN-18903"
    finally:
        await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        await conn.close()


@pytest.mark.integration
async def test_a_stale_redelivery_is_refused_by_the_conflict_arm() -> None:
    """The guard is SQL, so there is nowhere else this can be proven."""
    conn = await _connect_or_skip()
    try:
        await _setup(conn)
        await _upsert(conn, observed_at=_T0)
        stale = await _upsert(
            conn, observed_at=_T0 - timedelta(minutes=5), cause_code="product_failed"
        )
        assert stale == [], "an older redelivery must not overwrite a newer row"

        newer = await _upsert(
            conn, observed_at=_T0 + timedelta(minutes=5), cause_code="product_failed"
        )
        assert len(newer) == 1, "positive control: a newer event does write"

        row = await conn.fetchrow(
            f"SELECT cause_code FROM {_SCHEMA}.ci_attempt_outcome"
        )
        assert row is not None
        assert row["cause_code"] == "product_failed"
    finally:
        await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        await conn.close()


@pytest.mark.integration
async def test_every_enum_member_is_accepted_by_the_check_constraint() -> None:
    """The vocabulary and the schema, pinned against each other on a real server.

    A seventh member added to the enum without widening the migration writes a
    row the database refuses, at runtime, on the lane. This is where that is
    caught instead.
    """
    conn = await _connect_or_skip()
    try:
        await _setup(conn)
        for index, code in enumerate(EnumMergeCheckReasonCode, start=1):
            returned = await _upsert(
                conn,
                observed_at=_T0,
                cause_code=str(code),
                check_name=f"check-{index}",
            )
            assert len(returned) == 1, f"{code} was refused by the check constraint"

        count = await conn.fetchval(
            f"SELECT count(*) FROM {_SCHEMA}.ci_attempt_outcome"
        )
        assert count == len(list(EnumMergeCheckReasonCode)) == 6
    finally:
        await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        await conn.close()


@pytest.mark.integration
async def test_an_unknown_cause_code_is_refused_by_the_database() -> None:
    """The negative control: the constraint is doing something."""
    conn = await _connect_or_skip()
    try:
        await _setup(conn)
        with pytest.raises(asyncpg.CheckViolationError):
            await _upsert(conn, observed_at=_T0, cause_code="not_a_real_cause")
    finally:
        await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        await conn.close()


@pytest.mark.integration
async def test_a_null_ticket_is_storable() -> None:
    """A row with no parseable ticket is written, not refused."""
    conn = await _connect_or_skip()
    try:
        await _setup(conn)
        returned = await _upsert(conn, observed_at=_T0, ticket_id=None)
        assert len(returned) == 1
        row = await conn.fetchrow(f"SELECT ticket_id FROM {_SCHEMA}.ci_attempt_outcome")
        assert row is not None
        assert row["ticket_id"] is None
    finally:
        await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        await conn.close()
