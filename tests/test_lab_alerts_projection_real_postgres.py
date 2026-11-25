# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real-Postgres proof of the lab alerts upsert.

The unit tests drive the writer against a keyed double, which accepts a bound parameter of any Python
type and applies ON CONFLICT by assumption. Only a real Postgres connection enforces the column types
through asyncpg's extended query protocol (TIMESTAMPTZ from an aware datetime, UUID from a uuid) and the
table's own checks (a cleared row carries its clear time, an open row never does, an owner is never blank).

It SKIPS (never ERRORs) without a reachable database and provisions its own throwaway schema, applying the
node's real 0000 migration into it. The harness mirrors ``tests/test_lab_container_memory_real_postgres.py``.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote_plus

import asyncpg
import pytest

from omnimarket.models.model_lab_alert_transitioned import ModelLabAlertTransitioned
from omnimarket.nodes.node_projection_lab_alerts.handlers.handler_lab_alerts_writer import (
    _COLUMNS,
    _SELECT_ROW,
    _UPSERT_ROW,
    _parameter,
)
from omnimarket.nodes.node_projection_lab_alerts.handlers.handler_projection_lab_alerts import (
    plan_lab_alert_fold,
)

_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_lab_alerts"
    / "migrations"
    / "0000_create_lab_alerts.sql"
)
_SCHEMA = "lab_alerts_projection_test"
_T0 = datetime(2026, 11, 24, 9, 0, tzinfo=UTC)


async def _connect_or_skip() -> asyncpg.Connection:
    secret = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    if not secret:
        pytest.skip(
            "INTEGRATION_POSTGRES_PASSWORD not set -- skipping lab alerts DB proof"
        )
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = int(os.environ.get("INTEGRATION_POSTGRES_PORT", "5432"))
    user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
    db = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
    dsn = f"postgresql://{quote_plus(user)}:{quote_plus(secret)}@{host}:{port}/{db}"
    try:
        return await asyncpg.connect(dsn)
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"no reachable Postgres for lab alerts proof: {exc}")


def _scoped(statement: str) -> str:
    return statement.replace("omninode_internal.", f"{_SCHEMA}.")


async def _setup(conn: asyncpg.Connection) -> None:
    await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
    await conn.execute(f"CREATE SCHEMA {_SCHEMA}")
    await conn.execute(_scoped(_MIGRATION.read_text(encoding="utf-8")))


def _transition(direction: str, at: datetime) -> ModelLabAlertTransitioned:
    raised = _T0
    return ModelLabAlertTransitioned.model_validate(
        {
            "source": "dev_tag_heartbeat",
            "subject": "lane-dev",
            "transition": direction,
            "severity": "SEV2",
            "owner": "lab-floor",
            "detail": "dev tag has not moved",
            "raised_at": raised if direction == "cleared" else at,
            "occurred_at": at,
        }
    )


async def _apply(
    conn: asyncpg.Connection, transition: ModelLabAlertTransitioned
) -> int:
    stored = await conn.fetch(_scoped(_SELECT_ROW), transition.alert_id)
    row = plan_lab_alert_fold(transition, dict(stored[0]) if stored else None)
    if row is None:
        return 0
    returned = await conn.fetch(
        _scoped(_UPSERT_ROW), *(_parameter(name, row.get(name)) for name in _COLUMNS)
    )
    return len(returned)


@pytest.mark.integration
async def test_a_raise_a_clear_and_a_replay_leave_one_row() -> None:
    conn = await _connect_or_skip()
    try:
        await _setup(conn)
        assert await _apply(conn, _transition("raised", _T0)) == 1
        cleared = _transition("cleared", _T0 + timedelta(minutes=5))
        assert await _apply(conn, cleared) == 1
        assert await _apply(conn, cleared) == 0
        assert await _apply(conn, _transition("raised", _T0)) == 0
        rows = await conn.fetch(f"SELECT * FROM {_SCHEMA}.lab_alerts")
        assert len(rows) == 1
        row = rows[0]
        assert row["state"] == "cleared"
        assert row["severity"] == "SEV2"
        assert row["owner"] == "lab-floor"
        assert row["raised_at"] == _T0
        assert row["cleared_at"] == _T0 + timedelta(minutes=5)
    finally:
        await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        await conn.close()


@pytest.mark.integration
async def test_the_table_refuses_an_open_row_with_a_clear_time_and_a_blank_owner() -> (
    None
):
    conn = await _connect_or_skip()
    try:
        await _setup(conn)
        good = plan_lab_alert_fold(_transition("raised", _T0), None)
        assert good is not None
        for broken in (
            {"cleared_at": _T0 + timedelta(minutes=1)},
            {"owner": "  "},
        ):
            row = {**good, **broken}
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.fetch(
                    _scoped(_UPSERT_ROW),
                    *(_parameter(name, row.get(name)) for name in _COLUMNS),
                )
    finally:
        await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        await conn.close()
