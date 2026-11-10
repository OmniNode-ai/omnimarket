# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21607: a delegate-skill terminal that failed acceptance lands as failed.

Real-Postgres twin of ``tests/unit/delegation/test_failed_acceptance_shows_failed_
omn21607.py``. The unit tests prove the reading; this one proves the row the
live writer stores, with column types enforced by Postgres, for a terminal that
says ``status="completed"`` beside ``quality_gate_passed=false`` and the
criterion it failed.

Uses the same disposable-schema harness as the OMN-15909 write-path gate, and
skips (never errors) when ``INTEGRATION_POSTGRES_PASSWORD`` / ``POSTGRES_PASSWORD``
names no reachable database.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from omnimarket.projection.runner import MessageMeta
from tests.test_omn15909_real_postgres_projection_write_path_gate import (
    _provisioned_runner,
)

_CRITERION = "the answer must name the file it changed"


def _terminal(*, correlation_id: str, passed: bool) -> dict[str, object]:
    payload: dict[str, object] = {
        "correlation_id": correlation_id,
        "tenant_id": "beta-business-proof",
        "task_type": "review",
        "status": "completed",
        "model_name": "qwen3-local",
        "response": "an answer",
        "quality_gate_passed": passed,
    }
    if not passed:
        payload["failed_acceptance_criteria"] = [_CRITERION]
    return payload


@pytest.mark.integration
class TestFailedAcceptanceRealPostgres:
    async def test_golden_a_passing_terminal_lands_as_passing(self) -> None:
        async with _provisioned_runner() as (runner, admin_conn, _schema):
            correlation_id = str(uuid4())

            ok = await runner.project_event(
                runner._topic_delegate_skill_completed,
                _terminal(correlation_id=correlation_id, passed=True),
                MessageMeta(partition=0, offset=0, fallback_id=correlation_id),
            )

            assert ok is True
            row = await admin_conn.fetchrow(
                "SELECT * FROM delegation_events WHERE correlation_id = $1",
                correlation_id,
            )
            assert row is not None
            assert row["terminal_ok"] is True
            assert row["quality_gate_passed"] is True
            assert row["terminal_failure_cause"] is None

    async def test_error_a_completed_terminal_that_failed_acceptance_lands_failed(
        self,
    ) -> None:
        async with _provisioned_runner() as (runner, admin_conn, _schema):
            correlation_id = str(uuid4())

            ok = await runner.project_event(
                runner._topic_delegate_skill_completed,
                _terminal(correlation_id=correlation_id, passed=False),
                MessageMeta(partition=0, offset=0, fallback_id=correlation_id),
            )

            assert ok is True
            row = await admin_conn.fetchrow(
                "SELECT * FROM delegation_events WHERE correlation_id = $1",
                correlation_id,
            )
            assert row is not None
            assert row["terminal_ok"] is False
            assert row["quality_gate_passed"] is False
            assert row["terminal_failure_cause"] == "quality_gate_refused"
            assert _CRITERION in row["quality_gate_detail"]
