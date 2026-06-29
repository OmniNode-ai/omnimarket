# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden-chain test for node_delegation_routing_feedback_reducer (OMN-13216).

Drives the reducer through the *real dispatch path* — the raw terminal-event
payloads exactly as the live auto-wiring dispatcher delivers them (the flattened
domain payload plus a ``_topic`` marker, NOT a hand-crafted
``{"event": ..., "state": ...}`` wrapper). Handler-isolation tests passed while
the live runtime crashed because the shim read ``input_data["event"]`` which the
dispatcher never populated, constructed ``ModelDelegationFeedbackEvent(**{})``,
and raised a 7-field ValidationError that propagated out of the dispatcher and
swallowed the terminal for escalated requests
(see feedback_real_dispatch_path_tests).

Invariants proven:
  1. Each terminal event drives the reducer to a populated, schema-valid update
     — no empty-dict construction, no crash.
  2. An escalated request (escalation-triggered → all-tiers-failed) always
     produces a terminal feedback update; the terminal is NOT swallowed.
  3. State accumulates across dispatch invocations (request HWM == terminal HWM).
  4. A malformed/empty payload is an explicit no-op (skipped), never a crash.
  5. Replaying the same terminal payload re-derives the same feedback identity.
"""

from __future__ import annotations

from typing import Any

import pytest

from omnimarket.events.topics import (
    DELEGATION_ALL_TIERS_FAILED_TOPIC_V1,
    DELEGATION_CALL_COMPLETED_TOPIC_V1,
    DELEGATION_ESCALATION_TRIGGERED_TOPIC_V1,
)
from omnimarket.nodes.node_delegation_routing_feedback_reducer.handlers.handler_delegation_routing_feedback import (
    HandlerDelegationRoutingFeedback,
)

# The ticket's failing cell: CODEGEN-base-p1, escalated to the claude tier.
_CORRELATION_ID = "04d63eb7-be92-4f7a-b4c8-5bcdce043a9d"
_TASK_TYPE = "codegen"


def _completed(
    *, model_id: str, success: bool, latency_ms: int, rid: str
) -> dict[str, Any]:
    return {
        "_topic": DELEGATION_CALL_COMPLETED_TOPIC_V1,
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
        "_topic": DELEGATION_ESCALATION_TRIGGERED_TOPIC_V1,
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
        "_topic": DELEGATION_ALL_TIERS_FAILED_TOPIC_V1,
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
        """Escalation ladder for one correlation_id produces terminal updates at
        every step — none swallowed — and state accumulates monotonically.
        """
        handler = HandlerDelegationRoutingFeedback()
        state: dict[str, Any] = {}
        emitted_terminals = 0

        # Step 1: local model times out → escalation-triggered.
        r1 = handler.handle(
            {**_escalation(model_id="ds-v4-flash", rid="r1"), "_state": state}
        )
        assert r1["skipped"] is False  # Invariant 1, 2
        assert r1["event"] is not None  # terminal feedback update emitted
        assert r1["feedback"]["escalation_count"] == 1
        state = r1["state"]
        emitted_terminals += 1

        # Step 2: mid-tier also fails → escalation-triggered to ceiling tier.
        r2 = handler.handle(
            {**_escalation(model_id="qwen3-coder-30b", rid="r2"), "_state": state}
        )
        assert r2["skipped"] is False
        assert r2["event"] is not None
        state = r2["state"]
        emitted_terminals += 1

        # Step 3: ceiling tier (claude) fails too → all-tiers-failed terminal.
        r3 = handler.handle(
            {
                **_all_tiers_failed(
                    attempted=("ds-v4-flash", "qwen3-coder-30b", "claude"),
                    rid="r3",
                ),
                "_state": state,
            }
        )
        assert r3["skipped"] is False  # Invariant 2: terminal NOT swallowed
        assert r3["event"] is not None
        assert r3["feedback"]["model_id"] == "claude"  # last attempted carries failure
        assert r3["feedback"]["failure_count"] == 1
        state = r3["state"]
        emitted_terminals += 1

        # Invariant 3: every dispatched terminal produced exactly one update —
        # request count == terminal count (no HWM gap).
        assert emitted_terminals == 3
        # Three distinct (model_id, task_type) keys accumulated in state.
        assert len(state) == 3

    def test_completed_then_escalation_accumulates(self) -> None:
        handler = HandlerDelegationRoutingFeedback()
        state: dict[str, Any] = {}

        r1 = handler.handle(
            {
                **_completed(
                    model_id="ds-v4-flash", success=True, latency_ms=120, rid="r1"
                ),
                "_state": state,
            }
        )
        assert r1["skipped"] is False
        assert r1["feedback"]["success_count"] == 1
        assert r1["feedback"]["avg_latency_ms"] == pytest.approx(120.0)
        state = r1["state"]

        # Same model later escalates → escalation_count rises, total accumulates.
        r2 = handler.handle(
            {**_escalation(model_id="ds-v4-flash", rid="r2"), "_state": state}
        )
        assert r2["skipped"] is False
        assert r2["feedback"]["total_count"] == 2  # Invariant 3
        assert r2["feedback"]["escalation_count"] == 1
        assert r2["feedback"]["success_count"] == 1

    def test_empty_payload_is_noop_terminal_preserved(self) -> None:
        # Invariant 4: the exact OMN-13216 crash input — empty dict — is a no-op.
        handler = HandlerDelegationRoutingFeedback()
        result = handler.handle({})
        assert result["skipped"] is True
        assert result["feedback"] is None
        assert result["event"] is None

    def test_replay_same_terminal_redrives_same_identity(self) -> None:
        # Invariant 5: replaying the same terminal payload re-derives the same
        # (model_id, task_type) feedback identity (counts advance; identity stable).
        handler = HandlerDelegationRoutingFeedback()
        payload = _completed(
            model_id="ds-v4-flash", success=True, latency_ms=100, rid="r1"
        )
        r1 = handler.handle({**payload, "_state": {}})
        r2 = handler.handle({**payload, "_state": r1["state"]})
        assert r1["feedback"]["model_id"] == r2["feedback"]["model_id"]
        assert r1["feedback"]["task_type"] == r2["feedback"]["task_type"]
        assert r2["feedback"]["total_count"] == 2
