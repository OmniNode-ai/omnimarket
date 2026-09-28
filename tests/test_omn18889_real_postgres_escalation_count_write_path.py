# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real-Postgres proof that the terminal row builder's new column write lands.

WHY A REAL DATABASE, and why the SQLite seam module next door is not enough.
``tests/test_omn18889_local_attempt_ladder_seam.py`` proves the value reaches
the adapter, against a real SQLite file. SQLite cannot prove the half this
module exists for: its adapter ADDS an unknown column on the fly and its
columns are typeless by default, so a write naming a column Postgres does not
have, or naming one whose declared type rejects the value, passes there and
fails in the deployed plane. ``escalation_count`` is declared ``INTEGER`` on
the Postgres side; this asserts Postgres itself accepts the write and returns
the value as an integer.

THE RED HALF IS MECHANICAL. ``project_delegate_skill_terminal`` never named
``escalation_count`` before OMN-18889, so the column was NULL on every row the
local path wrote -- 23,316 of them in the live local store. Here that is
reproduced by writing a terminal whose event carries a non-zero count and
asserting the stored value equals it; the pre-change builder returns NULL and
fails the equality. ``test_the_fixture_is_faithful`` is the positive control:
it proves the column really exists on the provisioned schema before any
behavioural assertion runs on it, so a schema that silently lacked the column
could not green this by having the write skipped.

WHAT THIS DOES NOT PROVE. It says nothing about which principal the deployed
writer connects as, and nothing about the attempt ladder's JSON shape -- the
SQLite module owns that. It proves one column, of one declared type, on the
statement the terminal builder actually issues.

SKIPS rather than ERRORs without a reachable database, matching the harness in
``tests/test_writer_tenant_isolation_omn14898.py``.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import quote, quote_plus
from uuid import uuid4

import asyncpg
import pytest

#: Every test here needs a real Postgres. The decorator is repeated on each
#: function as well as declared here, because the write-path gate reads the
#: literal ``@pytest.mark.integration`` text out of the diff.
pytestmark = pytest.mark.integration

_MIGRATIONS_DIR = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_delegation"
    / "migrations"
)

_COLUMN_UNDER_TEST = "escalation_count"

#: ``CREATE INDEX CONCURRENTLY`` refuses to run inside a transaction block, and
#: asyncpg sends a multi-statement migration as one. CONCURRENTLY is a
#: production-apply concern (it avoids locking a live table) and has no bearing
#: on the column shape under test, so it is stripped for the throwaway schema.
#: Same treatment as ``tests/test_omn15909_real_postgres_projection_write_path_gate.py``.
_CONCURRENT_INDEX = re.compile(r"\bCREATE\s+INDEX\s+CONCURRENTLY\b", re.IGNORECASE)


def _password() -> str:
    return os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )


def _parts() -> tuple[str, str, str, str]:
    return (
        os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost"),
        os.environ.get("INTEGRATION_POSTGRES_PORT", "5432"),
        os.environ.get("INTEGRATION_POSTGRES_USER", "postgres"),
        os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra"),
    )


async def _connect_or_skip() -> asyncpg.Connection:
    password = _password()
    if not password:
        pytest.skip(
            "POSTGRES_PASSWORD not set -- skipping the escalation_count column proof"
        )
    host, port, user, db = _parts()
    dsn = f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{db}"
    try:
        return await asyncpg.connect(dsn)
    except (OSError, asyncpg.PostgresError) as exc:  # pragma: no cover - infra
        pytest.skip(f"no reachable Postgres for the escalation_count proof: {exc}")


def _sync_dsn_for_schema(schema: str) -> str:
    host, port, user, db = _parts()
    options = quote(f"-c search_path={schema},public")
    return (
        f"postgresql://{quote_plus(user)}:{quote_plus(_password())}@{host}:{port}/{db}"
        f"?options={options}"
    )


async def _provision(connection: asyncpg.Connection, schema: str) -> None:
    """Apply the node's migration chain into a throwaway schema.

    A disposable schema rather than the shared one, so concurrent runs on the
    same database never collide on the table.
    """
    await connection.execute(f'CREATE SCHEMA "{schema}"')
    await connection.execute(f'SET search_path TO "{schema}", public')
    for migration in sorted(_MIGRATIONS_DIR.glob("*.sql")):
        sql = migration.read_text(encoding="utf-8")
        await connection.execute(_CONCURRENT_INDEX.sub("CREATE INDEX", sql))


async def _declared_columns(
    connection: asyncpg.Connection, schema: str
) -> dict[str, str]:
    rows = await connection.fetch(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = $1 AND table_name = 'delegation_events'",
        schema,
    )
    return {row["column_name"]: row["data_type"] for row in rows}


@pytest.mark.integration
async def test_the_fixture_is_faithful() -> None:
    """Positive control: the provisioned schema really declares the column.

    Without this, a schema missing ``escalation_count`` would make the
    behavioural test below green for the wrong reason -- the adapter would
    simply never be asked for it.
    """
    connection = await _connect_or_skip()
    schema = f"omn18889_control_{uuid4().hex[:10]}"
    try:
        await _provision(connection, schema)
        columns = await _declared_columns(connection, schema)
        assert _COLUMN_UNDER_TEST in columns, sorted(columns)
        assert columns[_COLUMN_UNDER_TEST] == "integer", columns[_COLUMN_UNDER_TEST]
    finally:
        await connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await connection.close()


@pytest.mark.integration
async def test_the_terminal_row_builder_persists_the_escalation_count() -> None:
    """The write the local path issues, against the declared INTEGER column."""
    from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
        HandlerProjectionDelegation,
    )
    from omnimarket.projection.postgres_sync_database import (
        PostgresSyncProjectionAdapter,
    )

    connection = await _connect_or_skip()
    schema = f"omn18889_write_{uuid4().hex[:10]}"
    correlation_id = uuid4()
    try:
        await _provision(connection, schema)

        payload: dict[str, object] = {
            "status": "completed",
            "correlation_id": str(correlation_id),
            "task_type": "document",
            "provider": "local",
            "model_name": "model-local",
            "prompt_text": "the prompt as the customer typed it",
            "response": "the answer",
            "quality_gate_passed": True,
            "quality_gates_failed": [],
            "error_message": "",
            "tenant_id": "omninode",
            "escalation_count": 2,
            "attempts": [
                {
                    "tier": "local",
                    "backend_id": "backend-local",
                    "model_id": "model-local",
                    "quality_gate_passed": False,
                    "quality_score": 0.4,
                    "cost_usd": 0.0,
                    "acceptance_decision": "climb",
                    "acceptance_reason": "deterministic_floor_failed",
                },
                {
                    "tier": "claude",
                    "backend_id": "backend-claude",
                    "model_id": "model-claude",
                    "quality_gate_passed": True,
                    "quality_score": 0.9,
                    "cost_usd": 0.0,
                    "acceptance_decision": "accept",
                    "acceptance_reason": "quality_bar_met",
                },
            ],
            "metrics": {
                "input_tokens": 11,
                "output_tokens": 22,
                "total_tokens": 33,
                "latency_ms": 44,
                "cost_usd": 0.0,
                "cost_savings_usd": 0.5,
            },
        }
        adapter = PostgresSyncProjectionAdapter(_sync_dsn_for_schema(schema))
        try:
            payload["_db"] = adapter
            HandlerProjectionDelegation().handle(payload)
        finally:
            close = getattr(adapter, "close", None)
            if callable(close):
                close()

        stored = await connection.fetchrow(
            f'SELECT escalation_count, attempt_history FROM "{schema}".delegation_events '
            "WHERE correlation_id = $1",
            str(correlation_id),
        )
        assert stored is not None, "the terminal write produced no row"
        assert stored["escalation_count"] == 2
        # The ladder rides the same write; a row carrying the count but an
        # empty ladder would mean only half the seam landed.
        assert stored["attempt_history"] is not None
    finally:
        await connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await connection.close()


# --- OMN-18889 score half (plan row G2) -----------------------------------
#
# The same seam, two more columns. ``actual_score`` and ``required_bar`` are
# declared NUMERIC on the Postgres side; SQLite would accept any value in
# them, so only this module can prove the declared type takes the write. The
# known-bad input is an unscored terminal: it must store NULL, never 0.

_SCORE_COLUMNS = ("actual_score", "required_bar")


def _terminal_payload(
    correlation_id: object,
    *,
    status: str,
    actual_score: float | None,
    required_bar: float | None,
) -> dict[str, object]:
    return {
        "status": status,
        "correlation_id": str(correlation_id),
        "task_type": "document",
        "provider": "local",
        "model_name": "model-local",
        "prompt_text": "the prompt as the customer typed it",
        "response": "the answer" if status == "completed" else "",
        "quality_gate_passed": status == "completed",
        "quality_gates_failed": [] if status == "completed" else ["transport"],
        "error_message": "" if status == "completed" else "upstream exploded",
        "tenant_id": "omninode",
        "escalation_count": 0,
        "attempts": [],
        "actual_score": actual_score,
        "required_bar": required_bar,
        "metrics": {
            "input_tokens": 11,
            "output_tokens": 22,
            "total_tokens": 33,
            "latency_ms": 44,
            "cost_usd": 0.0,
            "cost_savings_usd": 0.0,
        },
    }


@pytest.mark.integration
async def test_the_score_columns_exist_as_numeric() -> None:
    """Positive control for the two score columns, as above for the count."""
    connection = await _connect_or_skip()
    schema = f"omn18889_scorectl_{uuid4().hex[:10]}"
    try:
        await _provision(connection, schema)
        columns = await _declared_columns(connection, schema)
        for column in _SCORE_COLUMNS:
            assert column in columns, sorted(columns)
            assert columns[column] in {"numeric", "double precision", "real"}, (
                column,
                columns[column],
            )
    finally:
        await connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await connection.close()


@pytest.mark.integration
async def test_the_terminal_row_builder_persists_scores_and_null_when_unscored() -> (
    None
):
    """A scored terminal stores its numbers; an unscored one stores NULL."""
    from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
        HandlerProjectionDelegation,
    )
    from omnimarket.projection.postgres_sync_database import (
        PostgresSyncProjectionAdapter,
    )

    connection = await _connect_or_skip()
    schema = f"omn18889_score_{uuid4().hex[:10]}"
    scored_id = uuid4()
    unscored_id = uuid4()
    try:
        await _provision(connection, schema)
        adapter = PostgresSyncProjectionAdapter(_sync_dsn_for_schema(schema))
        try:
            for payload in (
                _terminal_payload(
                    scored_id, status="completed", actual_score=0.873, required_bar=0.8
                ),
                _terminal_payload(
                    unscored_id, status="failed", actual_score=None, required_bar=None
                ),
            ):
                payload["_db"] = adapter
                HandlerProjectionDelegation().handle(payload)
        finally:
            close = getattr(adapter, "close", None)
            if callable(close):
                close()

        rows = {
            row["correlation_id"]: row
            for row in await connection.fetch(
                f"SELECT correlation_id::text AS correlation_id, actual_score, "
                f'required_bar FROM "{schema}".delegation_events',
            )
        }
        scored = rows[str(scored_id)]
        assert float(scored["actual_score"]) == pytest.approx(0.873)
        assert float(scored["required_bar"]) == pytest.approx(0.8)
        unscored = rows[str(unscored_id)]
        assert unscored["actual_score"] is None
        assert unscored["required_bar"] is None
    finally:
        await connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await connection.close()


@pytest.mark.integration
async def test_a_later_unscored_terminal_does_not_erase_a_graded_zero() -> None:
    """An unscored terminal names neither score column, so a graded 0.0 stays.

    The preserve step treats 0.0 and NULL alike, so a builder that named the
    column as NULL would overwrite a genuine zero with NULL on the UPDATE.
    """
    from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
        HandlerProjectionDelegation,
    )
    from omnimarket.projection.postgres_sync_database import (
        PostgresSyncProjectionAdapter,
    )

    connection = await _connect_or_skip()
    schema = f"omn18889_zero_{uuid4().hex[:10]}"
    correlation_id = uuid4()
    try:
        await _provision(connection, schema)
        adapter = PostgresSyncProjectionAdapter(_sync_dsn_for_schema(schema))
        try:
            for payload in (
                _terminal_payload(
                    correlation_id, status="failed", actual_score=0.0, required_bar=0.8
                ),
                _terminal_payload(
                    correlation_id,
                    status="failed",
                    actual_score=None,
                    required_bar=None,
                ),
            ):
                payload["_db"] = adapter
                HandlerProjectionDelegation().handle(payload)
        finally:
            close = getattr(adapter, "close", None)
            if callable(close):
                close()

        stored = await connection.fetchrow(
            f'SELECT actual_score, required_bar FROM "{schema}".delegation_events '
            "WHERE correlation_id = $1",
            str(correlation_id),
        )
        assert stored is not None
        assert stored["actual_score"] is not None
        assert float(stored["actual_score"]) == 0.0
        assert float(stored["required_bar"]) == pytest.approx(0.8)
    finally:
        await connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await connection.close()
