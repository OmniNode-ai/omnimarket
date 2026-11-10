# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18534: apply 0069 and read the real terminal turn count.

Real-Postgres twin of the turn-count unit tests, using the live migration set
and DelegationProjectionRunner.project_event(). The database is the
INTEGRATION_POSTGRES_* one CI configures; the module skips without a configured,
reachable server.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from omnimarket.projection.runner import MessageMeta
from tests.test_omn15909_real_postgres_projection_write_path_gate import (
    _MIGRATIONS_DIR,
    _provisioned_runner,
)
from tests.test_omn18928_terminal_outcome_projection_real_postgres import (
    _TENANT,
    _quota_terminal,
    _wire,
)
from tests.unit.delegation.test_terminal_turn_count_omn18534 import (
    _escalated_skill_terminal,
    _refused_before_dispatch_skill_terminal,
)

_SELECT = "SELECT turn_count FROM delegation_events WHERE correlation_id = $1"
_MIGRATION = _MIGRATIONS_DIR / "0069_delegation_events_turn_count.sql"


@pytest.mark.integration
@pytest.mark.parametrize(
    ("make_terminal", "expected"),
    [
        (_escalated_skill_terminal, 3),
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
