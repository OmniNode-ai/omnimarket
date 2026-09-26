# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19448 AC1: the delegation_events row carries the terminal's own cause.

The live writer is ``DelegationProjectionRunner`` (the projection-delegation
writer container). Two terminal families co-write one row per correlation:

* the canonical ``delegation-completed/failed`` terminal, which the writer
  converted without its ``terminal_failure_cause``, so the row's cause was NULL
  even for a quota refusal whose terminal names ``provider_quota_exhausted``;
* the delegate-skill terminal, whose cause the writer re-derived from the
  attempt ladder, so a terminal saying ``provider_error`` over a rate-limited
  ladder stored ``provider_quota_exhausted``, or NULL when the ladder held no
  quota refusal (lab run ``5e0488e0``, 2026-09-25: wire ``provider_error``,
  row NULL).

The canonical terminals are produced by the REAL orchestrator and driven
through the REAL ``project_event()`` against a disposable schema migrated with
this node's full migration set. Real Postgres, never a mock. The harness SKIPS
without ``INTEGRATION_POSTGRES_PASSWORD`` and a reachable server.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_result import (
    ModelDelegationFailed,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_inference_response_data import (
    ModelInferenceResponseData,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_result import (
    ModelQualityGateResult,
)
from omnimarket.projection.runner import MessageMeta
from tests.test_omn15909_real_postgres_projection_write_path_gate import (
    _provisioned_runner,
)
from tests.test_omn18928_terminal_outcome_projection_real_postgres import (
    _LADDER_EXHAUSTED,
    _completed_terminal,
    _quota_terminal,
    _routed,
    _terminal,
    _wire,
)

_SELECT = (
    "SELECT terminal_failure_cause, operational_outcome, content_verdict, "
    "quality_gate_passed FROM delegation_events WHERE correlation_id = $1"
)


def _gate_refused_terminal() -> ModelDelegationFailed:
    cid = uuid4()
    handler = _routed(cid)
    handler.workflows[cid].escalation_count = _LADDER_EXHAUSTED
    handler.handle_inference_response(
        ModelInferenceResponseData(
            correlation_id=cid,
            content="nope",
            model_used="qwen3-coder-30b",
            latency_ms=10,
            prompt_tokens=1,
            completion_tokens=1,
            total_tokens=2,
            llm_call_id="chatcmpl-omn19448-gate",
        )
    )
    terminal = _terminal(
        handler.handle_gate_result(
            ModelQualityGateResult(
                correlation_id=cid,
                passed=False,
                quality_score=0.1,
                failure_reasons=["too short"],
            )
        )
    )
    assert isinstance(terminal, ModelDelegationFailed)
    return terminal


def _skill_failed_payload(cid: str, cause: str | None) -> dict[str, object]:
    payload: dict[str, object] = {
        "status": "failed",
        "correlation_id": cid,
        "task_type": "summarization",
        "quality_gate_passed": False,
        "quality_score": 0.0,
        "model_name": "glm-5.3-flash",
        "attempts_count": 1,
        "attempts": [
            {
                "tier": "local",
                "backend_id": "qwen3-local",
                "model_id": "Qwen3.8-27B",
                "quality_gate_passed": False,
                "failure_class": "rate_limited",
            }
        ],
        "tenant_id": "beta-business-proof",
    }
    if cause is not None:
        payload["terminal_failure_cause"] = cause
    return payload


@pytest.mark.integration
class TestTheRowCarriesTheTerminalsCause:
    async def test_a_canonical_quota_terminal_stores_its_cause(self) -> None:
        terminal = _quota_terminal()
        payload = _wire(terminal)
        assert payload["terminal_failure_cause"] == "provider_quota_exhausted"

        async with _provisioned_runner() as (runner, admin_conn, _schema):
            cid = str(terminal.correlation_id)
            assert await runner.project_event(
                runner._topic_delegation_failed,
                payload,
                MessageMeta(partition=0, offset=0, fallback_id=cid),
            )
            row = await admin_conn.fetchrow(_SELECT, cid)
            assert row is not None
            assert row["terminal_failure_cause"] == "provider_quota_exhausted"
            assert row["operational_outcome"] == "provider_quota"

    async def test_a_canonical_gate_refusal_stores_the_quality_gate_member(
        self,
    ) -> None:
        # AC1's own falsifier: a terminal whose cause is the quality-gate member.
        # The orchestrator stamps it once OMN-19004 lands; the wire is set here the
        # way that producer sets it, and the row must carry it unchanged.
        terminal = _gate_refused_terminal()
        payload = _wire(terminal)
        payload["terminal_failure_cause"] = "quality_gate_refused"

        async with _provisioned_runner() as (runner, admin_conn, _schema):
            cid = str(terminal.correlation_id)
            assert await runner.project_event(
                runner._topic_delegation_failed,
                payload,
                MessageMeta(partition=0, offset=1, fallback_id=cid),
            )
            row = await admin_conn.fetchrow(_SELECT, cid)
            assert row is not None
            assert row["terminal_failure_cause"] == "quality_gate_refused"
            assert row["operational_outcome"] == "quality_rejected"
            assert row["content_verdict"] == "unusable"
            assert row["quality_gate_passed"] is False

    async def test_a_skill_terminal_cause_wins_over_the_ladder(self) -> None:
        cid = str(uuid4())
        async with _provisioned_runner() as (runner, admin_conn, _schema):
            assert await runner.project_event(
                runner._topic_delegate_skill_failed,
                _skill_failed_payload(cid, "provider_error"),
                MessageMeta(partition=0, offset=2, fallback_id=cid),
            )
            row = await admin_conn.fetchrow(_SELECT, cid)
            assert row is not None
            assert row["terminal_failure_cause"] == "provider_error", (
                "the terminal's own cause, not the ladder's quota guess"
            )

    async def test_a_skill_terminal_without_a_cause_keeps_the_ladder_fallback(
        self,
    ) -> None:
        # Positive control: an older producer that names no cause still gets the
        # ladder-derived quota cause.
        cid = str(uuid4())
        async with _provisioned_runner() as (runner, admin_conn, _schema):
            assert await runner.project_event(
                runner._topic_delegate_skill_failed,
                _skill_failed_payload(cid, None),
                MessageMeta(partition=0, offset=3, fallback_id=cid),
            )
            row = await admin_conn.fetchrow(_SELECT, cid)
            assert row is not None
            assert row["terminal_failure_cause"] == "provider_quota_exhausted"

    async def test_a_completed_terminal_stores_no_cause(self) -> None:
        terminal = _completed_terminal()
        async with _provisioned_runner() as (runner, admin_conn, _schema):
            cid = str(terminal.correlation_id)
            assert await runner.project_event(
                runner._topic_delegation_completed,
                _wire(terminal),
                MessageMeta(partition=0, offset=4, fallback_id=cid),
            )
            row = await admin_conn.fetchrow(_SELECT, cid)
            assert row is not None
            assert row["terminal_failure_cause"] is None
