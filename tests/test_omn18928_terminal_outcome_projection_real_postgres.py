# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18928 (K1): the projected row carries the terminal's outcome and verdict.

K1's projection proof is that the ``delegation_events`` row the delegation
projection writes for a terminal carries that terminal's
``operational_outcome`` and ``content_verdict`` unchanged, and that a terminal
with no graded response lands with no score. The columns are created by
migration 0045; the writer copies the two fields onto the row.

The terminals are not hand-written dictionaries. Each one is produced by the
REAL orchestrator from a controlled inference response and serialized the way
the outbox serializes it, then driven through the REAL
``DelegationProjectionRunner.project_event()`` against a disposable schema
migrated with this node's full live migration set. Real Postgres, never a
mock: a mock accepts a key for a column that does not exist, and a missing
column is exactly the regression this guards.

Harness: ``tests/test_omn15909_real_postgres_projection_write_path_gate.py``,
reused rather than copied. It SKIPS without ``INTEGRATION_POSTGRES_PASSWORD``
(or ``POSTGRES_PASSWORD``) and a reachable server.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import NAMESPACE_DNS, UUID, uuid4, uuid5

import pytest

from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    HandlerDelegationWorkflow,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_result import (
    ModelDelegationCompleted,
    ModelDelegationFailed,
    ModelDelegationResult,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_inference_response_data import (
    ModelInferenceResponseData,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_result import (
    ModelQualityGateResult,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_routing_decision import (
    ModelRoutingDecision,
)
from omnimarket.projection.runner import MessageMeta
from tests.test_omn15909_real_postgres_projection_write_path_gate import (
    _provisioned_runner,
)

# A tenant with a canonical UUID mapping (see the OMN-15909 harness).
_TENANT = "beta-business-proof"
_LADDER_EXHAUSTED = 99


def _routed(cid: UUID) -> HandlerDelegationWorkflow:
    handler = HandlerDelegationWorkflow()
    handler.handle_delegation_request(
        ModelDelegationRequest(
            prompt="Write unit tests for verify_registration.py",
            task_type="test",  # type: ignore[arg-type]
            correlation_id=cid,
            emitted_at=datetime.now(UTC),
        )
    )
    handler.handle_routing_decision(
        ModelRoutingDecision(
            correlation_id=cid,
            task_type="test",
            selected_model="qwen3-coder-30b",
            selected_backend_id=uuid5(
                NAMESPACE_DNS, "omninode.ai/backends/qwen3-coder-30b"
            ),
            endpoint_url="http://lab-llm.invalid:8000/v1/chat/completions",
            cost_tier="low",
            max_context_tokens=65536,
            max_tokens=4096,
            system_prompt="You are a test generation assistant.",
            rationale="Task 'test' routed to qwen3-coder-30b.",
        )
    )
    return handler


def _terminal(events: list[object]) -> ModelDelegationResult:
    terminals = [e for e in events if isinstance(e, ModelDelegationResult)]
    assert len(terminals) == 1, events
    return terminals[0]


def _quota_terminal() -> ModelDelegationResult:
    cid = uuid4()
    handler = _routed(cid)
    handler.workflows[cid].escalation_count = _LADDER_EXHAUSTED
    return _terminal(
        handler.handle_inference_response(
            ModelInferenceResponseData(
                correlation_id=cid,
                content="",
                model_used="qwen3-coder-30b",
                latency_ms=50,
                error_message="HTTP 429: rate limit exceeded",
            )
        )
    )


def _completed_terminal() -> ModelDelegationResult:
    cid = uuid4()
    handler = _routed(cid)
    handler.handle_inference_response(
        ModelInferenceResponseData(
            correlation_id=cid,
            content="def test_verify_registration():\n    assert True",
            model_used="qwen3-coder-30b",
            latency_ms=1200,
            prompt_tokens=100,
            completion_tokens=50,
            total_tokens=150,
            llm_call_id="chatcmpl-omn18928-pg",
        )
    )
    return _terminal(
        handler.handle_gate_result(
            ModelQualityGateResult(correlation_id=cid, passed=True, quality_score=0.9)
        )
    )


def _wire(terminal: ModelDelegationResult) -> dict[str, object]:
    """The terminal as the outbox serializes it, stamped with a mapped tenant."""
    payload = terminal.model_dump(mode="json")
    payload["tenant_id"] = _TENANT
    return payload


@pytest.mark.integration
class TestTheRowCarriesTheTerminalsPair:
    async def test_a_quota_terminal_projects_its_outcome_and_no_score(self) -> None:
        terminal = _quota_terminal()
        assert isinstance(terminal, ModelDelegationFailed)
        payload = _wire(terminal)
        assert "quality_score" not in payload, "precondition: no score on the wire"

        async with _provisioned_runner() as (runner, admin_conn, _schema):
            cid = str(terminal.correlation_id)
            ok = await runner.project_event(
                runner._topic_delegation_failed,
                payload,
                MessageMeta(partition=0, offset=0, fallback_id=cid),
            )

            assert ok is True
            row = await admin_conn.fetchrow(
                "SELECT operational_outcome, content_verdict, actual_score, "
                "quality_gate_passed FROM delegation_events "
                "WHERE correlation_id = $1",
                cid,
            )
            assert row is not None, "the failed terminal must land a row"
            assert row["operational_outcome"] == "provider_quota"
            assert row["content_verdict"] == "not_applicable"
            assert row["actual_score"] is None, (
                "a provider quota refusal is not a model that scored zero"
            )
            assert row["quality_gate_passed"] is False

    async def test_a_completed_terminal_projects_completed_and_usable(self) -> None:
        # Positive control: a graded completion keeps its score and its pair.
        terminal = _completed_terminal()
        assert isinstance(terminal, ModelDelegationCompleted)

        async with _provisioned_runner() as (runner, admin_conn, _schema):
            cid = str(terminal.correlation_id)
            ok = await runner.project_event(
                runner._topic_delegation_completed,
                _wire(terminal),
                MessageMeta(partition=0, offset=1, fallback_id=cid),
            )

            assert ok is True
            row = await admin_conn.fetchrow(
                "SELECT operational_outcome, content_verdict, actual_score "
                "FROM delegation_events WHERE correlation_id = $1",
                cid,
            )
            assert row is not None
            assert row["operational_outcome"] == "completed"
            assert row["content_verdict"] == "usable"
            assert float(row["actual_score"]) == pytest.approx(0.9)

    async def test_a_legacy_terminal_without_the_pair_projects_nulls(self) -> None:
        # A terminal produced before K1 still projects; its pair is NULL, which
        # the 0045 readers count as an ordinary row.
        terminal = _completed_terminal()
        payload = _wire(terminal)
        payload.pop("operational_outcome")
        payload.pop("content_verdict")

        async with _provisioned_runner() as (runner, admin_conn, _schema):
            cid = str(terminal.correlation_id)
            ok = await runner.project_event(
                runner._topic_delegation_completed,
                payload,
                MessageMeta(partition=0, offset=2, fallback_id=cid),
            )

            assert ok is True
            row = await admin_conn.fetchrow(
                "SELECT operational_outcome, content_verdict "
                "FROM delegation_events WHERE correlation_id = $1",
                cid,
            )
            assert row is not None
            assert row["operational_outcome"] is None
            assert row["content_verdict"] is None
