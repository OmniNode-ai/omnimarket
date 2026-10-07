# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19448 AC2: apply 0058 and read real terminal requested model and timings.

Real-Postgres twin of the timing/requested-model unit tests, using the live
migration set and DelegationProjectionRunner.project_event(). The database is
the INTEGRATION_POSTGRES_* one CI configures; the module skips without a
configured, reachable server.
"""

from __future__ import annotations

import pytest

from omnimarket.projection.runner import MessageMeta
from tests.test_omn15909_real_postgres_projection_write_path_gate import (
    _MIGRATIONS_DIR,
    _provisioned_runner,
)
from tests.test_omn18928_terminal_outcome_projection_real_postgres import (
    _quota_terminal,
    _wire,
)
from tests.unit.delegation.test_terminal_timing_and_requested_model_omn19448 import (
    _timed_skill_terminal,
)

_SELECT = (
    "SELECT requested_model, queue_wait_ms, execution_ms FROM delegation_events "
    "WHERE correlation_id = $1"
)
_MIGRATION = _MIGRATIONS_DIR / "0058_delegation_events_requested_model_and_timing.sql"


@pytest.mark.integration
@pytest.mark.parametrize("path", ["skill", "canonical"])
@pytest.mark.parametrize("queue_wait_ms", [1234, 0])
async def test_terminal_stores_requested_model_and_timings_and_keeps_sparse_evidence(
    path: str, queue_wait_ms: int
) -> None:
    payload = (
        _timed_skill_terminal()
        if path == "skill"
        else {
            **_wire(_quota_terminal()),
            "requested_model": "first-requested-model",
            "execution_duration_ms": 567,
        }
    )
    payload["queue_wait_ms"] = queue_wait_ms
    cid = str(payload["correlation_id"])
    async with _provisioned_runner() as (runner, admin_conn, _schema):
        # Re-apply the migration to prove its idempotent shape on Postgres.
        await admin_conn.execute(_MIGRATION.read_text())
        topic = (
            runner._topic_delegate_skill_completed
            if path == "skill"
            else runner._topic_delegation_failed
        )
        assert await runner.project_event(
            topic, payload, MessageMeta(partition=0, offset=0, fallback_id=cid)
        )
        row = await admin_conn.fetchrow(_SELECT, cid)
        assert row is not None
        assert row["requested_model"] == "first-requested-model"
        assert row["queue_wait_ms"] == queue_wait_ms
        assert row["execution_ms"] == 567
        payload.pop("requested_model", None)
        payload.pop("queue_wait_ms")
        payload.pop("execution_duration_ms")
        if path == "skill":
            payload["attempts"] = []
        assert await runner.project_event(
            topic, payload, MessageMeta(partition=0, offset=1, fallback_id=cid)
        )
        assert await admin_conn.fetchrow(_SELECT, cid) == row


@pytest.mark.integration
async def test_unmeasured_terminal_and_pre_migration_row_read_nulls() -> None:
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
        assert tuple(row.values()) == (None, None, None)
        await admin_conn.execute(
            "ALTER TABLE delegation_events DROP COLUMN requested_model, "
            "DROP COLUMN queue_wait_ms, DROP COLUMN execution_ms"
        )
        await admin_conn.execute(_MIGRATION.read_text())
        assert await admin_conn.fetchrow(_SELECT, cid) == row
