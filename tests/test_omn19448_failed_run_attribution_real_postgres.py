# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19448: a failed run never reads unknown, through the real async writer.

Uses the established INTEGRATION_POSTGRES_* harness and disposable migrated
schemas, so PostgreSQL enforces the projection's column types.

Rule 1: a rung's verdict never creates the run record.
Rule 2: every run record, passed or failed, names its model, backend and host.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from omnimarket.projection.runner import MessageMeta
from tests.test_omn15905_delegation_projection_writer_seam import (
    _real_quality_gate_result_payload,
)
from tests.test_omn15909_real_postgres_projection_write_path_gate import (
    _provisioned_runner,
)
from tests.test_omn19559_terminal_precedence_real_postgres import (
    _MODEL,
    _skill_success,
    _skill_timeout,
)


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("cause", ["timeout", "runtime_shutdown"])
async def test_a_run_that_failed_before_any_try_reads_not_routed(cause: str) -> None:
    """OMN-19448 rule 2: a run with no try names why it was not routed."""
    cid = str(uuid4())
    payload: dict[str, object] = {
        "tenant_id": "beta-business-proof",
        "status": "failed",
        "correlation_id": cid,
        "task_type": "summarization",
        "model_name": "",
        "quality_gate_passed": False,
        "terminal_failure_cause": cause,
        "error_message": "the run ended before any try",
        "execution_duration_ms": 4321,
        "metrics": {"latency_ms": 0},
    }
    async with _provisioned_runner() as (runner, connection, _schema):
        assert await runner.project_event(
            runner._topic_delegate_skill_failed,
            payload,
            MessageMeta(partition=0, offset=0, fallback_id=cid),
        )
        row = await connection.fetchrow(
            "SELECT model_name, backend_id, host, latency_ms "
            "FROM delegation_events WHERE correlation_id = $1",
            cid,
        )
        assert row is not None
        assert row["model_name"] == f"not routed ({cause})"
        assert row["backend_id"] == "not routed"
        assert row["host"] == "not routed"
        assert row["latency_ms"] == 4321


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_rung_verdict_creates_no_run_row() -> None:
    """OMN-19448 rule 1: a verdict that arrives before its terminal writes nothing."""
    cid = str(uuid4())
    async with _provisioned_runner() as (runner, connection, _schema):
        assert await runner.project_event(
            runner._topic_quality_gate_result,
            _real_quality_gate_result_payload(correlation_id=cid, passed=False),
            MessageMeta(partition=0, offset=0, fallback_id=cid),
        )
        count = await connection.fetchval(
            "SELECT count(*) FROM delegation_events WHERE correlation_id = $1", cid
        )
        assert count == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_late_handler_timeout_inherits_no_model_into_its_precedence() -> None:
    """OMN-19448 attribution runs after precedence, so a success stays whole.

    The stored success's model is not copied onto the late timeout before the
    precedence check, so the timeout still reads as an unevidenced handler
    failure and the evidenced success is kept, never read as not routed.
    """
    cid = str(uuid4())
    async with _provisioned_runner() as (runner, connection, _schema):
        assert await runner.project_event(
            runner._topic_delegate_skill_completed,
            _skill_success(cid),
            MessageMeta(partition=0, offset=0, fallback_id=cid),
        )
        assert await runner.project_event(
            runner._topic_delegate_skill_failed,
            _skill_timeout(cid),
            MessageMeta(partition=0, offset=1, fallback_id=cid),
        )
        row = await connection.fetchrow(
            "SELECT terminal_ok, terminal_failure_cause, model_name, backend_id "
            "FROM delegation_events WHERE correlation_id = $1",
            cid,
        )
        assert row is not None
        assert row["terminal_ok"] is True
        assert row["terminal_failure_cause"] is None
        assert row["model_name"] == _MODEL
        assert row["backend_id"] != "not routed"
