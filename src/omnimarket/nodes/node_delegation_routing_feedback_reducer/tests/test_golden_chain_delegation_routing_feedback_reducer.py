# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain through realistic typed terminal payloads."""

from __future__ import annotations

from typing import Any

import pytest

from omnimarket.nodes.node_delegation_routing_feedback_reducer.handlers.handler_delegation_routing_feedback import (
    HandlerDelegationRoutingFeedback,
)
from omnimarket.nodes.node_delegation_routing_feedback_reducer.models.model_delegation_terminal_payload import (
    ModelDelegationTerminalPayload,
)

# The ticket's failing cell: CODEGEN-base-p1, escalated to the claude tier.
_CORRELATION_ID = "04d63eb7-be92-4f7a-b4c8-5bcdce043a9d"
_TASK_TYPE = "codegen"


def _completed(
    *, model_id: str, success: bool, latency_ms: int, rid: str
) -> dict[str, Any]:
    return {
        "correlation_id": _CORRELATION_ID,
        "causation_id": "cause",
        "request_id": rid,
        "task_type": _TASK_TYPE,
        "task_id": None,
        "model_id": model_id,
        "model_tier": "cheap_local",
        "success": success,
        "latency_ms": latency_ms,
        "escalated_to": None,
    }


def _escalation(*, model_id: str, rid: str) -> dict[str, Any]:
    return {
        "correlation_id": _CORRELATION_ID,
        "causation_id": "cause",
        "request_id": rid,
        "task_type": _TASK_TYPE,
        "task_id": None,
        "model_id": model_id,
        "attempt_number": 1,
        "escalation_reason": "timed out",
        "next_model_id": "claude",
    }


def _all_tiers_failed(*, attempted: tuple[str, ...], rid: str) -> dict[str, Any]:
    return {
        "correlation_id": _CORRELATION_ID,
        "causation_id": "cause",
        "request_id": rid,
        "task_type": _TASK_TYPE,
        "task_id": None,
        "attempted_models": attempted,
    }


@pytest.mark.unit
class TestGoldenChainDelegationRoutingFeedback:
    def test_escalation_ladder_terminal_not_swallowed(self) -> None:
        handler = HandlerDelegationRoutingFeedback()
        payloads = [
            _escalation(model_id="fixture-model-a", rid="r1"),
            _escalation(model_id="qwen3-coder-30b", rid="r2"),
            _all_tiers_failed(
                attempted=("fixture-model-a", "qwen3-coder-30b", "claude"), rid="r3"
            ),
        ]
        updates = [
            handler.handle(ModelDelegationTerminalPayload(**payload))
            for payload in payloads
        ]
        assert len(updates) == 3
        for update in updates:
            assert update is not None
            assert update.feedback.total_count == 1
        assert updates[0] is not None
        assert updates[0].feedback.escalation_count == 1
        assert updates[1] is not None
        assert updates[1].feedback.escalation_count == 1
        assert updates[2] is not None
        assert updates[2].feedback.model_id == "claude"
        assert updates[2].feedback.failure_count == 1
        assert (
            len({update.feedback.model_id for update in updates if update is not None})
            == 3
        )
        for payload in payloads:
            repeated = handler.handle(ModelDelegationTerminalPayload(**payload))
            assert repeated is not None
            assert repeated.feedback.total_count == 2

    def test_completed_then_escalation_accumulates(self) -> None:
        handler = HandlerDelegationRoutingFeedback()
        first = handler.handle(
            ModelDelegationTerminalPayload(
                **_completed(
                    model_id="fixture-model-a", success=True, latency_ms=120, rid="r1"
                )
            )
        )
        assert first is not None
        assert first.feedback.success_count == 1
        assert first.feedback.avg_latency_ms == pytest.approx(120.0)
        second = handler.handle(
            ModelDelegationTerminalPayload(
                **_escalation(model_id="fixture-model-a", rid="r2")
            )
        )
        assert second is not None
        assert second.feedback.total_count == 2
        assert second.feedback.escalation_count == 1
        assert second.feedback.success_count == 1

    def test_empty_payload_is_noop_terminal_preserved(self) -> None:
        assert (
            HandlerDelegationRoutingFeedback().handle(
                ModelDelegationTerminalPayload(**{})
            )
            is None
        )

    def test_replay_same_terminal_redrives_same_identity(self) -> None:
        handler = HandlerDelegationRoutingFeedback()
        request = ModelDelegationTerminalPayload(
            **_completed(
                model_id="fixture-model-a", success=True, latency_ms=100, rid="r1"
            )
        )
        first, second = handler.handle(request), handler.handle(request)
        assert first is not None
        assert second is not None
        assert first.feedback.model_id == second.feedback.model_id
        assert first.feedback.task_type == second.feedback.task_type
        assert second.feedback.total_count == 2
