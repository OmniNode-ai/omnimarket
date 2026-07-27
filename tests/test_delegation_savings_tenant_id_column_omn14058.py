# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-14058: prove ``delegation_events``/``savings_estimates`` have a real
``tenant_id`` column, against real Postgres.

Context
-------
The interim per-tenant identity stamp (companion PRs omnibase_core#1404 /
omnimarket#1648) has ``HandlerProjectionDelegation`` and
``HandlerProjectionSavings`` write ``row["tenant_id"]`` on delegation/savings
projection rows. Neither ``delegation_events`` (migration ``0007``) nor
``savings_estimates`` (migration ``074``) had a ``tenant_id`` column before
OMN-14058's ``0022``/``080`` migrations added it. ``PostgresSyncProjectionAdapter.
upsert()`` (``omnimarket/src/omnimarket/projection/postgres_sync_database.py``)
builds its ``INSERT`` column list directly from ``row.keys()``, so the first
write with ``ONEX_TENANT_ID`` set raised ``column "tenant_id" does not exist``
against real Postgres.

Two existing test surfaces mask this entirely, by design (neither has a real
schema to violate):

  * ``InmemoryDatabaseAdapter`` (``protocol_database.py``) — the adapter unit
    tests for the handlers exercise this fake, which stores whatever dict keys
    it is given.
  * ``test_postgres_sync_database_omn14015.py`` — drives a *fake* psycopg2
    connection that captures the generated SQL string but never executes it,
    so it proves the SQL is well-formed, never that the target column exists.

This module is the real-Postgres proof that closes that gap:

  1. static structure of the committed ``0022``/``080`` migrations (always
     runs, no DB);
  2. real-Postgres execution of the actual migration SQL (base CREATE TABLE +
     the new tenant_id ALTER) followed by the actual production write path
     (``PostgresSyncProjectionAdapter.upsert()``, not the in-memory fake and
     not a monkeypatched connection) -- skips without a live DB.
"""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import quote, quote_plus

import asyncpg
import pytest
import yaml

from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    CONFLICT_KEY as DELEGATION_CONFLICT_KEY,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    TABLE as DELEGATION_TABLE,
)
from omnimarket.nodes.node_projection_savings.handlers.handler_projection_savings import (
    CONFLICT_KEY as SAVINGS_CONFLICT_KEY,
)
from omnimarket.nodes.node_projection_savings.handlers.handler_projection_savings import (
    TABLE as SAVINGS_TABLE,
)
from omnimarket.projection.postgres_sync_database import PostgresSyncProjectionAdapter

_DELEGATION_MIGRATIONS_DIR = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_delegation"
    / "migrations"
)
_SAVINGS_MIGRATIONS_DIR = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_savings"
    / "migrations"
)
_DELEGATION_BASE_SQL = _DELEGATION_MIGRATIONS_DIR / "0007_delegation_events.sql"
_DELEGATION_TENANT_SQL = (
    _DELEGATION_MIGRATIONS_DIR / "0022_delegation_events_tenant_id.sql"
)
_SAVINGS_BASE_SQL = _SAVINGS_MIGRATIONS_DIR / "074_create_savings_estimates.sql"
_SAVINGS_TENANT_SQL = _SAVINGS_MIGRATIONS_DIR / "080_savings_estimates_tenant_id.sql"

_DELEGATION_CONTRACT = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_delegation"
    / "contract.yaml"
)
_SAVINGS_CONTRACT = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_savings"
    / "contract.yaml"
)
_BUDGET_STATE_TOPIC = "onex.snapshot.projection.delegation.budget-state.v1"
_SAVINGS_APPLIED_TOPIC = "onex.evt.omnimarket.projection-savings-applied.v1"
_SAVINGS_OVERVIEW_TOPIC = "onex.snapshot.projection.cost.savings-overview.v1"

_DEFAULT_TENANT = "omninode"


# ---------------------------------------------------------------------------
# 1. Static structure: the committed migrations exist and add the column.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_delegation_tenant_id_migration_exists() -> None:
    assert _DELEGATION_TENANT_SQL.is_file(), (
        f"missing delegation_events tenant_id migration: {_DELEGATION_TENANT_SQL}"
    )


@pytest.mark.unit
def test_savings_tenant_id_migration_exists() -> None:
    assert _SAVINGS_TENANT_SQL.is_file(), (
        f"missing savings_estimates tenant_id migration: {_SAVINGS_TENANT_SQL}"
    )


@pytest.mark.unit
def test_delegation_tenant_id_migration_adds_defaulted_not_null_column() -> None:
    sql = _DELEGATION_TENANT_SQL.read_text(encoding="utf-8")
    assert "ALTER TABLE delegation_events" in sql
    assert (
        "ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 'omninode'" in sql
    ), "migration must be idempotent (IF NOT EXISTS) and default existing rows"


@pytest.mark.unit
def test_savings_tenant_id_migration_adds_defaulted_not_null_column() -> None:
    sql = _SAVINGS_TENANT_SQL.read_text(encoding="utf-8")
    assert "ALTER TABLE savings_estimates" in sql
    assert (
        "ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 'omninode'" in sql
    ), "migration must be idempotent (IF NOT EXISTS) and default existing rows"


# ---------------------------------------------------------------------------
# 2. Real-Postgres execution of the actual migrations + the actual write path.
# ---------------------------------------------------------------------------


async def _connect_or_skip() -> asyncpg.Connection:
    """Connect to a reachable Postgres or skip.

    Self-contained (mirrors ``test_projection_delegation_tier_distribution_
    omn13662.py``'s ``_connect_or_skip``) so the test SKIPS -- never ERRORs --
    when ``POSTGRES_PASSWORD`` is set but no DB is reachable (the common
    CI-without-DB shape).
    """
    password = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    if not password:
        pytest.skip("POSTGRES_PASSWORD not set — skipping tenant_id column DB proof")
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
        pytest.skip(f"no reachable Postgres for tenant_id column DB proof: {exc}")


def _sync_dsn_for_schema(schema: str) -> str:
    """Build the psycopg2 DSN ``PostgresSyncProjectionAdapter`` connects with.

    Pins ``search_path`` to the isolated test schema (via the standard
    ``options`` connection parameter) so the adapter's own
    ``psycopg2.connect()`` call resolves ``delegation_events``/
    ``savings_estimates`` against the throwaway fixture tables created below,
    never the real projection tables.
    """
    password = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = os.environ.get("INTEGRATION_POSTGRES_PORT", "5432")
    user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
    db = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
    # NOTE: use ``quote`` (space -> %20), NOT ``quote_plus`` (space -> +).
    # libpq parses the URI ``options`` value per RFC 3986 and does NOT decode
    # ``+`` back to a space, so a ``quote_plus``-encoded ``-c search_path=...``
    # arrives as ``-c+search_path=...`` and fails with
    # ``unrecognized configuration parameter "+search_path"`` (OMN-14167).
    options = quote(f"-c search_path={schema},public")
    return (
        f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{db}"
        f"?options={options}"
    )


@pytest.mark.integration
async def test_delegation_events_upsert_stamps_tenant_id_on_real_postgres() -> None:
    """Apply migration 0007 + 0022, then UPSERT via the actual production
    adapter -- proving the tenant_id write no longer raises `column "tenant_id"
    does not exist` and that a value round-trips correctly."""
    conn = await _connect_or_skip()
    schema = "omn14058_delegation_tenant_test"

    await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    await conn.execute(f"CREATE SCHEMA {schema}")
    try:
        await conn.execute(f"SET search_path TO {schema}, public")
        await conn.execute(_DELEGATION_BASE_SQL.read_text(encoding="utf-8"))
        await conn.execute(_DELEGATION_TENANT_SQL.read_text(encoding="utf-8"))

        adapter = PostgresSyncProjectionAdapter(_sync_dsn_for_schema(schema))

        # A caller-supplied tenant_id overrides the column default on INSERT --
        # this is the exact call HandlerProjectionDelegation makes.
        ok = adapter.upsert(
            DELEGATION_TABLE,
            DELEGATION_CONFLICT_KEY,
            {"correlation_id": "omn14058-stamped", "tenant_id": "acme-corp"},
        )
        assert ok is True

        stamped_row = await conn.fetchrow(
            "SELECT tenant_id FROM delegation_events WHERE correlation_id = $1",
            "omn14058-stamped",
        )
        assert stamped_row is not None
        assert stamped_row["tenant_id"] == "acme-corp"

        # Omitting tenant_id (the "no tenant resolved" branch -- the key is
        # never written as NULL) must still land under the column default.
        ok = adapter.upsert(
            DELEGATION_TABLE,
            DELEGATION_CONFLICT_KEY,
            {"correlation_id": "omn14058-unstamped"},
        )
        assert ok is True

        defaulted_row = await conn.fetchrow(
            "SELECT tenant_id FROM delegation_events WHERE correlation_id = $1",
            "omn14058-unstamped",
        )
        assert defaulted_row is not None
        assert defaulted_row["tenant_id"] == _DEFAULT_TENANT
    finally:
        await conn.execute("SET search_path TO public")
        await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await conn.close()


@pytest.mark.integration
async def test_savings_estimates_upsert_stamps_tenant_id_on_real_postgres() -> None:
    """Apply migration 074 + 080, then UPSERT via the actual production
    adapter -- proving the tenant_id write no longer raises `column "tenant_id"
    does not exist` and that a value round-trips correctly."""
    conn = await _connect_or_skip()
    schema = "omn14058_savings_tenant_test"

    await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    await conn.execute(f"CREATE SCHEMA {schema}")
    try:
        await conn.execute(f"SET search_path TO {schema}, public")
        await conn.execute(_SAVINGS_BASE_SQL.read_text(encoding="utf-8"))
        await conn.execute(_SAVINGS_TENANT_SQL.read_text(encoding="utf-8"))

        adapter = PostgresSyncProjectionAdapter(_sync_dsn_for_schema(schema))

        row: dict[str, Any] = {
            "event_timestamp": "2026-07-08T00:00:00+00:00",
            "session_id": "omn14058-session",
            "model_local": "qwen3-coder-30b",
            "model_cloud_baseline": "claude-opus-4",
            "local_cost_usd": Decimal("0.01"),
            "cloud_cost_usd": Decimal("0.05"),
            "savings_usd": Decimal("0.04"),
            "tenant_id": "acme-corp",
        }
        ok = adapter.upsert(SAVINGS_TABLE, SAVINGS_CONFLICT_KEY, row)
        assert ok is True

        stamped_row = await conn.fetchrow(
            "SELECT tenant_id FROM savings_estimates WHERE session_id = $1",
            "omn14058-session",
        )
        assert stamped_row is not None
        assert stamped_row["tenant_id"] == "acme-corp"
    finally:
        await conn.execute("SET search_path TO public")
        await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await conn.close()


# ---------------------------------------------------------------------------
# 3. state-coverage-gate (OMN-13781) baseline conformance.
#
# This diff adds migrations under node_projection_delegation/'s and
# node_projection_savings/'s own directories, which the gate treats as
# "directly touched" and promotes their pre-existing baselined coverage gaps
# (scripts/validation/state_coverage_baseline.txt) from WARN to hard FAIL --
# unrelated to tenant_id, verified via `git stash` (gate passes on the
# unmodified tree). omnimarket#1648 hit and resolved the identical promotion
# for these same two nodes with real contract-conformance assertions rather
# than bypassing the gate; these mirror that precedent.
# ---------------------------------------------------------------------------


def test_delegation_budget_state_topic_declared_in_snapshot_exposures() -> None:
    data = yaml.safe_load(_DELEGATION_CONTRACT.read_text(encoding="utf-8"))
    exposed_topics = [e["topic"] for e in data["projection_api"]["exposures"]]
    assert _BUDGET_STATE_TOPIC in exposed_topics, (
        f"{_BUDGET_STATE_TOPIC} must be declared as a projection_api exposure "
        "on node_projection_delegation"
    )


def test_savings_applied_topic_is_the_declared_terminal_event() -> None:
    data = yaml.safe_load(_SAVINGS_CONTRACT.read_text(encoding="utf-8"))
    assert data["terminal_event"] == _SAVINGS_APPLIED_TOPIC


def test_savings_overview_topic_declared_in_snapshot_exposures() -> None:
    data = yaml.safe_load(_SAVINGS_CONTRACT.read_text(encoding="utf-8"))
    exposed_topics = [e["topic"] for e in data["projection_api"]["exposures"]]
    assert _SAVINGS_OVERVIEW_TOPIC in exposed_topics, (
        f"{_SAVINGS_OVERVIEW_TOPIC} must be declared as a projection_api "
        "exposure on node_projection_savings"
    )
