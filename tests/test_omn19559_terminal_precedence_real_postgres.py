# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19559 on the live writer: a delegation row never contradicts itself.

The live writer is ``DelegationProjectionRunner`` (the projection-delegation
writer container, ``handler_delegation.py``). Two terminal families co-write one
row per correlation: the canonical ``delegation-completed/failed`` terminal
names the operational outcome and content verdict, the delegate-skill terminal
names ``terminal_ok`` and the cause. On the lab dev lane 248 rows read
``terminal_ok=false`` with a cause while still ``completed``/``usable``, and a
handler-local timeout from a duplicate delivery turned an evidenced success
into a failure.

Driven through the REAL ``project_event()`` against a disposable schema
migrated with this node's full migration set. Real Postgres, never a mock. The
harness SKIPS without ``INTEGRATION_POSTGRES_PASSWORD`` and a reachable server.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from omnimarket.projection.runner import MessageMeta
from tests.test_omn15909_real_postgres_projection_write_path_gate import (
    _provisioned_runner,
)
from tests.test_omn18928_terminal_outcome_projection_real_postgres import (
    _TENANT,
    _completed_terminal,
    _wire,
)

_SELECT = (
    "SELECT terminal_ok, terminal_failure_cause, operational_outcome, "
    "content_verdict, model_name, quality_gate_passed "
    "FROM delegation_events WHERE correlation_id = $1"
)
_MODEL = "Qwen3.8-27B"


def _skill_timeout(cid: str) -> dict[str, object]:
    """A delegate-skill handler's own budget expired: no model, no attempts."""
    return {
        "status": "timeout",
        "correlation_id": cid,
        "task_type": "summarization",
        "quality_gate_passed": False,
        "quality_score": 0.0,
        "model_name": "",
        "error_message": "delegation exceeded the handler execution budget",
        "terminal_failure_cause": "timeout",
        "attempts": [],
        "tenant_id": _TENANT,
    }


def _skill_success(cid: str) -> dict[str, object]:
    """An outer success with a model and one accepted attempt."""
    return {
        "status": "completed",
        "correlation_id": cid,
        "task_type": "summarization",
        "quality_gate_passed": True,
        "quality_score": 1.0,
        "model_name": _MODEL,
        "response": "the answer",
        "attempts_count": 1,
        "attempts": [
            {
                "tier": "local",
                "backend_id": "local-heavy-reasoning",
                "model_id": _MODEL,
                "quality_gate_passed": True,
                "failure_class": None,
            }
        ],
        "tenant_id": _TENANT,
    }


@pytest.mark.integration
class TestTheRowNeverContradictsItself:
    async def test_inner_completed_then_handler_timeout_reads_timeout(self) -> None:
        terminal = _completed_terminal()
        cid = str(terminal.correlation_id)
        async with _provisioned_runner() as (runner, admin_conn, _schema):
            assert await runner.project_event(
                runner._topic_delegation_completed,
                _wire(terminal),
                MessageMeta(partition=0, offset=0, fallback_id=cid),
            )
            assert await runner.project_event(
                runner._topic_delegate_skill_failed,
                _skill_timeout(cid),
                MessageMeta(partition=0, offset=1, fallback_id=cid),
            )
            row = await admin_conn.fetchrow(_SELECT, cid)
            assert row is not None
            assert row["terminal_ok"] is False
            assert row["terminal_failure_cause"] == "timeout"
            assert row["operational_outcome"] == "timeout"
            assert row["content_verdict"] == "not_applicable"

    async def test_handler_timeout_then_inner_completed_reads_timeout(self) -> None:
        terminal = _completed_terminal()
        cid = str(terminal.correlation_id)
        async with _provisioned_runner() as (runner, admin_conn, _schema):
            assert await runner.project_event(
                runner._topic_delegate_skill_failed,
                _skill_timeout(cid),
                MessageMeta(partition=0, offset=0, fallback_id=cid),
            )
            assert await runner.project_event(
                runner._topic_delegation_completed,
                _wire(terminal),
                MessageMeta(partition=0, offset=1, fallback_id=cid),
            )
            row = await admin_conn.fetchrow(_SELECT, cid)
            assert row is not None
            assert row["terminal_ok"] is False
            assert row["operational_outcome"] == "timeout"
            assert row["content_verdict"] == "not_applicable"

    async def test_late_handler_timeout_keeps_the_evidenced_success(self) -> None:
        cid = str(uuid4())
        async with _provisioned_runner() as (runner, admin_conn, _schema):
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
            row = await admin_conn.fetchrow(_SELECT, cid)
            assert row is not None
            assert row["terminal_ok"] is True
            assert row["terminal_failure_cause"] is None
            assert row["model_name"] == _MODEL
            assert row["quality_gate_passed"] is True

    async def test_success_after_a_handler_timeout_wins(self) -> None:
        cid = str(uuid4())
        async with _provisioned_runner() as (runner, admin_conn, _schema):
            assert await runner.project_event(
                runner._topic_delegate_skill_failed,
                _skill_timeout(cid),
                MessageMeta(partition=0, offset=0, fallback_id=cid),
            )
            assert await runner.project_event(
                runner._topic_delegate_skill_completed,
                _skill_success(cid),
                MessageMeta(partition=0, offset=1, fallback_id=cid),
            )
            row = await admin_conn.fetchrow(_SELECT, cid)
            assert row is not None
            assert row["terminal_ok"] is True
            assert row["terminal_failure_cause"] is None
            assert row["operational_outcome"] == "completed"
