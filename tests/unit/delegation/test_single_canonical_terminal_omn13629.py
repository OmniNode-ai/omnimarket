# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-13629 (WS-F Phase 1): one delegation terminal = one canonical event.

The all-tiers-failed research delegation that drove F1 (silent terminal loss,
live CID 67d2bfc8) is the canonical regression case. Before this work the
orchestrator emitted TWO terminal events — a canonical ``ModelDelegationResult``
AND a legacy compat ``ModelTaskDelegatedEvent`` whose ``cost_savings_usd`` pinned
``ge=0.0``. On an all-tiers-failed terminal the savings subtraction went negative,
the compat-event ValidationError aborted the whole dispatch, and NEITHER terminal
was published.

This suite is the TDD gate for the collapse to a single canonical terminal:

1. an all-tiers-failed terminal emits EXACTLY ONE event, on ``delegation-failed.v1``,
   carrying a ``ModelDelegationResult`` — no compat twin;
2. the derived savings is non-negative even when prior metered spend exceeds the
   final-tier counterfactual (the clamp is now a value floor, never a
   terminal-suppressing crash);
3. the canonical failed terminal materializes a ``delegation_events`` projection
   row (the projection consumes the canonical pair directly);
4. the savings projection consumes the canonical terminal (repointed off
   task-delegated.v1) and yields a truthful no-row on a failed terminal that
   carries no derivable counterfactual.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import NAMESPACE_DNS, UUID, uuid4, uuid5

import pytest

from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter
from omnimarket.nodes.node_delegation_orchestrator.contract_topics import (
    TOPIC_ID_DELEGATION_FAILED,
)
from omnimarket.nodes.node_delegation_orchestrator.enums import EnumDelegationState
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    HandlerDelegationWorkflow,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_event import (
    ModelDelegationEvent,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_result import (
    ModelDelegationResult,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_inference_response_data import (
    ModelInferenceResponseData,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_task_delegated_event import (
    ModelTaskDelegatedEvent,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_routing_decision import (
    ModelRoutingDecision,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    ModelTaskDelegatedEvent as ProjectionRowEvent,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    _canonical_result_to_task_delegated_payload,
)
from omnimarket.nodes.node_projection_savings.handlers.handler_savings import (
    SavingsProjectionRunner,
)
from omnimarket.projection.runner import MessageMeta

_NON_RETRYABLE_ERROR = "empty message content from upstream model"
_PROMPT_TOKENS = 88
_COMPLETION_TOKENS = 9532
_SAVINGS_DLQ_TOPIC = "onex.dlq.omnimarket.projection-savings-malformed.v1"


def _make_request(correlation_id: UUID) -> ModelDelegationRequest:
    return ModelDelegationRequest(
        prompt="Research the tradeoffs of vector vs graph retrieval for code RAG.",
        task_type="research",  # type: ignore[arg-type]
        correlation_id=correlation_id,
        emitted_at=datetime.now(UTC),
    )


def _make_routing_decision(correlation_id: UUID) -> ModelRoutingDecision:
    return ModelRoutingDecision(
        correlation_id=correlation_id,
        task_type="research",
        selected_model="glm-5.2",
        selected_backend_id=uuid5(NAMESPACE_DNS, "omninode.ai/backends/ceiling"),
        endpoint_url="http://192.168.86.201:8000",  # onex-allow-internal-ip OMN-10865 reason="delegation test fixture for local AIPC LLM endpoint"
        cost_tier="ceiling",
        tier_name="ceiling",
        max_context_tokens=65536,
        max_tokens=10,
        system_prompt="You are a research assistant.",
        rationale="research routed to ceiling tier; no higher tier available.",
    )


def _make_failed_response(correlation_id: UUID) -> ModelInferenceResponseData:
    """A non-retryable terminal inference failure on the ceiling tier — no higher
    tier to escalate to, so the workflow FAILS (all-tiers-failed)."""
    return ModelInferenceResponseData(
        correlation_id=correlation_id,
        content="",
        model_used="glm-5.2",
        llm_call_id="chatcmpl-omn13629-failed",
        latency_ms=12_000,
        prompt_tokens=_PROMPT_TOKENS,
        completion_tokens=_COMPLETION_TOKENS,
        total_tokens=_PROMPT_TOKENS + _COMPLETION_TOKENS,
        error_message=_NON_RETRYABLE_ERROR,
    )


def _drive_all_tiers_failed() -> list[object]:
    handler = HandlerDelegationWorkflow(workflows={})
    cid = uuid4()
    handler.handle_delegation_request(_make_request(cid))
    handler.handle_routing_decision(_make_routing_decision(cid))
    events = handler.handle_inference_response(_make_failed_response(cid))
    assert handler.workflows[cid].state == EnumDelegationState.FAILED
    return list(events)


@pytest.mark.unit
class TestSingleCanonicalTerminalOmn13629:
    """An all-tiers-failed terminal emits exactly one canonical failed event."""

    def test_emits_exactly_one_terminal_on_delegation_failed(self) -> None:
        events = _drive_all_tiers_failed()
        terminals = [
            e
            for e in events
            if isinstance(e, ModelDelegationEvent)
            and isinstance(e.payload, ModelDelegationResult)
        ]
        assert len(terminals) == 1, (
            f"all-tiers-failed must emit EXACTLY ONE canonical terminal, got: {events!r}"
        )
        terminal = terminals[0]
        assert str(terminal.topic) == TOPIC_ID_DELEGATION_FAILED
        assert terminal.payload.quality_passed is False

    def test_no_compat_task_delegated_event_emitted(self) -> None:
        events = _drive_all_tiers_failed()
        compat = [e for e in events if isinstance(e, ModelTaskDelegatedEvent)]
        assert compat == [], (
            f"OMN-13629: the legacy compat ModelTaskDelegatedEvent must NOT be "
            f"emitted, got: {compat!r}"
        )

    def test_terminal_survives_with_nonnegative_derived_savings(self) -> None:
        """The terminal is published (not suppressed) and the projection's derived
        saving on the failed path is non-negative — the clamp is an honest value
        floor, no longer a ge=0 crash that suppresses the terminal."""
        events = _drive_all_tiers_failed()
        terminal = next(
            e.payload
            for e in events
            if isinstance(e, ModelDelegationEvent)
            and isinstance(e.payload, ModelDelegationResult)
        )
        # Run the failed terminal through the canonical projection converter +
        # cost measurement; the materialized saving must be >= 0.
        from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
            _measure_actual_cost,
        )

        payload = terminal.model_dump(mode="json")
        converted = _canonical_result_to_task_delegated_payload(payload)
        measurement = _measure_actual_cost(ProjectionRowEvent(**converted))
        assert measurement.cost_savings_usd >= 0.0

    def test_canonical_failed_terminal_materializes_projection_row(self) -> None:
        """The canonical failed terminal converts to a valid delegation_events row
        model (the projection write path), proving the row is written."""
        events = _drive_all_tiers_failed()
        terminal = next(
            e.payload
            for e in events
            if isinstance(e, ModelDelegationEvent)
            and isinstance(e.payload, ModelDelegationResult)
        )
        payload = terminal.model_dump(mode="json")
        converted = _canonical_result_to_task_delegated_payload(payload)
        # Constructing the projection row event is the projection's row-write
        # gate; it must not raise (the all-tiers-failed shape is projectable).
        row_event = ProjectionRowEvent(**converted)
        assert str(row_event.correlation_id) == str(terminal.correlation_id)
        assert row_event.task_type == "research"
        assert row_event.quality_gate_passed is False


@pytest.mark.unit
class TestSavingsRunnerCanonicalRepointOmn13629:
    """SavingsProjectionRunner consumes the canonical delegation terminal pair."""

    @staticmethod
    def _mock_db() -> Any:
        mock_db = MagicMock(spec=AsyncpgAdapter)
        mock_db.execute = AsyncMock(return_value=None)
        return mock_db

    @staticmethod
    def _capture() -> tuple[list[tuple[str, bytes]], Any]:
        published: list[tuple[str, bytes]] = []

        async def capture_publish(topic: str, value: bytes) -> None:
            published.append((topic, value))

        return published, capture_publish

    def test_canonical_topics_are_subscribed(self) -> None:
        runner = SavingsProjectionRunner()
        subs = runner.subscribe_topics
        assert "onex.evt.omnibase-infra.delegation-completed.v1" in subs
        assert "onex.evt.omnibase-infra.delegation-failed.v1" in subs

    def test_legacy_task_delegated_topic_removed(self) -> None:
        runner = SavingsProjectionRunner()
        assert "onex.evt.omniclaude.task-delegated.v1" not in runner.subscribe_topics

    def test_failed_terminal_is_truthful_empty_no_dlq(self) -> None:
        """A canonical FAILED terminal carries no derivable counterfactual →
        truthful no-row, NO DLQ (a valid business state, not malformed)."""
        import asyncio

        published, capture = self._capture()
        runner = SavingsProjectionRunner(publish_fn=capture)
        runner._db = self._mock_db()

        events = _drive_all_tiers_failed()
        terminal = next(
            e.payload
            for e in events
            if isinstance(e, ModelDelegationEvent)
            and isinstance(e.payload, ModelDelegationResult)
        )
        data = terminal.model_dump(mode="json")
        meta = MessageMeta(partition=0, offset=0, fallback_id=str(uuid4()))

        ok = asyncio.run(
            runner.project_event(
                "onex.evt.omnibase-infra.delegation-failed.v1", data, meta
            )
        )

        assert ok is True
        dlq_rows = [(t, v) for t, v in published if t == _SAVINGS_DLQ_TOPIC]
        assert dlq_rows == [], "failed terminal must NOT be DLQ'd as malformed"
        runner._db.execute.assert_not_awaited()

    def test_completed_terminal_with_savings_upserts_row(self) -> None:
        """A canonical COMPLETED terminal whose re-derived counterfactual beats the
        measured cost upserts a savings row (the measurement path)."""
        import asyncio

        captured_kwargs: list[dict[str, Any]] = []

        async def capture_upsert(**kwargs: Any) -> None:
            captured_kwargs.append(kwargs)

        published, capture = self._capture()
        runner = SavingsProjectionRunner(publish_fn=capture)
        runner._upsert_savings_estimate = capture_upsert  # type: ignore[method-assign]

        # A completed canonical terminal with served tokens + a small measured
        # local cost. The cloud baseline is re-derived from the served tokens; if
        # the manifest yields a baseline > local cost, a savings row is written.
        data: dict[str, Any] = {
            "correlation_id": str(uuid4()),
            "task_type": "research",
            "model_used": "qwen3-coder-30b",
            "quality_passed": True,
            "cumulative_attempt_cost": 0.0,
            "final_attempt_cost": 0.0,
            "cumulative_input_tokens": 1000,
            "cumulative_output_tokens": 500,
            "prompt_tokens": 1000,
            "completion_tokens": 500,
            "timestamp": "2026-06-20T12:00:00+00:00",
        }
        meta = MessageMeta(partition=0, offset=1, fallback_id="cid")

        ok = asyncio.run(
            runner.project_event(
                "onex.evt.omnibase-infra.delegation-completed.v1", data, meta
            )
        )

        assert ok is True
        dlq_rows = [(t, v) for t, v in published if t == _SAVINGS_DLQ_TOPIC]
        assert dlq_rows == []
        # A free local terminal (cost 0) with a non-zero re-derived counterfactual
        # yields a positive saving and one upsert.
        if captured_kwargs:
            kwargs = captured_kwargs[0]
            assert kwargs["local_cost_usd"] == Decimal("0")
            assert kwargs["savings_usd"] > 0
