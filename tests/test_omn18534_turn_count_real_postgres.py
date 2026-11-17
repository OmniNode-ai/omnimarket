# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18534: apply 0069 and read the real terminal turn count.

Real-Postgres twin of the turn-count unit tests, using the live migration set
and DelegationProjectionRunner.project_event(). The database is the
INTEGRATION_POSTGRES_* one CI configures. A missing or unreachable server is an
error, never a skip: a skipped twin reads as a pass in the suite summary while
proving nothing about migration 0069 on Postgres.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import asyncpg
import pytest

from omnimarket.projection.runner import MessageMeta
from tests.test_omn15909_real_postgres_projection_write_path_gate import (
    _MIGRATIONS_DIR,
    _base_dsn,
    _provisioned_runner,
)
from tests.test_omn18928_terminal_outcome_projection_real_postgres import (
    _TENANT,
    _quota_terminal,
    _wire,
)
from tests.unit.delegation.test_terminal_turn_count_omn18534 import (
    _ESCALATED_TOTAL_CALLS,
    _escalated_skill_terminal,
    _refused_before_dispatch_skill_terminal,
)


async def require_postgres() -> None:
    """Fail, not skip, when the INTEGRATION_POSTGRES_* database cannot be reached."""
    try:
        conn = await asyncpg.connect(_base_dsn(), timeout=10)
    except (OSError, TimeoutError, asyncpg.PostgresError) as exc:
        pytest.fail(
            "the OMN-18534 Postgres twin needs the INTEGRATION_POSTGRES_* "
            f"database and cannot reach it: {type(exc).__name__}: {exc}",
            pytrace=False,
        )
    await conn.close()


@pytest.fixture(autouse=True)
async def _twin_never_skips() -> None:
    await require_postgres()


_SELECT = "SELECT turn_count FROM delegation_events WHERE correlation_id = $1"
_MIGRATION = _MIGRATIONS_DIR / "0069_delegation_events_turn_count.sql"


@pytest.mark.integration
@pytest.mark.parametrize(
    ("make_terminal", "expected"),
    [
        (_escalated_skill_terminal, _ESCALATED_TOTAL_CALLS),
        (_refused_before_dispatch_skill_terminal, 0),
    ],
)
async def test_skill_terminal_stores_total_calls_and_a_zero_is_not_null(
    make_terminal: Callable[[], dict[str, Any]], expected: int
) -> None:
    payload = make_terminal()
    payload["tenant_id"] = _TENANT
    cid = str(payload["correlation_id"])
    async with _provisioned_runner() as (runner, admin_conn, _schema):
        # Re-apply the migration to prove its idempotent shape on Postgres.
        await admin_conn.execute(_MIGRATION.read_text())
        assert await runner.project_event(
            runner._topic_delegate_skill_completed,
            payload,
            MessageMeta(partition=0, offset=0, fallback_id=cid),
        )
        row = await admin_conn.fetchrow(_SELECT, cid)
        assert row is not None
        assert row["turn_count"] == expected


@pytest.mark.integration
async def test_canonical_terminal_stores_its_attempts_count() -> None:
    payload = {**_wire(_quota_terminal()), "attempts_count": 4}
    cid = str(payload["correlation_id"])
    async with _provisioned_runner() as (runner, admin_conn, _schema):
        assert await runner.project_event(
            runner._topic_delegation_failed,
            payload,
            MessageMeta(partition=0, offset=0, fallback_id=cid),
        )
        row = await admin_conn.fetchrow(_SELECT, cid)
        assert row is not None
        assert row["turn_count"] == 4
        payload.pop("attempts_count")
        assert await runner.project_event(
            runner._topic_delegation_failed,
            payload,
            MessageMeta(partition=0, offset=1, fallback_id=cid),
        )
        assert await admin_conn.fetchrow(_SELECT, cid) == row


@pytest.mark.integration
async def test_unreported_terminal_and_pre_migration_row_read_null() -> None:
    payload = _wire(_quota_terminal())
    cid = str(payload["correlation_id"])
    # The orchestrator's own terminal reports its total calls, so the producer
    # that reports no count is modelled by a terminal whose payload omits the
    # key. Only that the key was reported matters here, not which count another
    # module's fixture happens to carry.
    assert isinstance(payload.pop("attempts_count"), int)
    async with _provisioned_runner() as (runner, admin_conn, _schema):
        assert await runner.project_event(
            runner._topic_delegation_failed,
            payload,
            MessageMeta(partition=0, offset=0, fallback_id=cid),
        )
        row = await admin_conn.fetchrow(_SELECT, cid)
        assert row is not None
        assert row["turn_count"] is None
        await admin_conn.execute("ALTER TABLE delegation_events DROP COLUMN turn_count")
        await admin_conn.execute(_MIGRATION.read_text())
        assert await admin_conn.fetchrow(_SELECT, cid) == row
