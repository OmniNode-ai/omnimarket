# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declared-state coverage for node_pr_review_fsm_reducer (OMN-13674).

REDUCER archetype -> Variant B: the FSM reducer handler is registered on the
canonical in-memory bus (``EventBusInmemory`` via ``integration_event_bus``)
through ``LocalRuntimeBusAdapter`` (``drive_round_trip``). One phase-advance
command is published on the ``pr-review-bot-phase-advance.v1`` subscribe topic
and the advanced ``ModelPrReviewBotState`` projection republished on the
``pr-review-bot-fsm-state-updated.v1`` topic is asserted.

This suite closes the ``state_machine`` declared-state set from ``contract.yaml``
(``init -> fetch_diff -> review -> post_threads -> watch -> judge_verify ->
report -> done`` plus the 3-failure circuit breaker ``-> failed``) by asserting
the *literal* lowercase ``current_phase`` each fold lands in, and exercises the
REDUCER dimensions:

  * every declared success edge advances to the next phase,
  * a single failure retries the phase in place (``init`` stays ``init``),
  * the 3-failure circuit breaker trips to ``failed`` with an error message,
  * folding the same advance twice is deterministic (idempotent fold), and
  * advancing from a terminal phase is rejected at the handler boundary -> the
    adapter publishes NO projection (the negative control).

No live Kafka / .201 — fully in-process.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import pytest
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

from omnimarket.nodes.node_pr_review_fsm_reducer.handlers.handler_pr_review_fsm import (
    HandlerPrReviewFsm,
)
from omnimarket.nodes.node_pr_review_fsm_reducer.models.model_pr_review_advance_command import (
    ModelPrReviewAdvanceCommand,
)
from omnimarket.review.pr_review_fsm import ModelPrReviewBotState
from omnimarket.review.pr_review_io import EnumFsmPhase
from tests.integration._wave7_bus import drive_round_trip

_START_TOPIC = "onex.evt.omnimarket.pr-review-bot-phase-advance.v1"
_RESULT_TOPIC = "onex.evt.omnimarket.pr-review-bot-fsm-state-updated.v1"
_ADVANCE_EVENT_TYPE = "onex.evt.omnimarket.pr-review-bot-phase-advance.v1"


class _PrReviewFsmBusHandler:
    """Bus-facing shim: wraps the advance command in an envelope and returns the
    projected FSM state so the adapter republishes it to the output topic."""

    def __init__(self) -> None:
        self._handler = HandlerPrReviewFsm()

    async def handle(self, **payload: Any) -> ModelPrReviewBotState:
        command = ModelPrReviewAdvanceCommand.model_validate(payload)
        envelope: ModelEventEnvelope[ModelPrReviewAdvanceCommand] = ModelEventEnvelope(
            payload=command,
            correlation_id=command.state.correlation_id,
            event_type=_ADVANCE_EVENT_TYPE,
        )
        output = await self._handler.handle(envelope)
        projection = output.projections[0]
        assert isinstance(projection, ModelPrReviewBotState)
        return projection


def _state(
    current_phase: str,
    *,
    consecutive_failures: int = 0,
    correlation_id: Any = None,
) -> ModelPrReviewBotState:
    return ModelPrReviewBotState(
        correlation_id=correlation_id or uuid4(),
        pr_number=42,
        repo="OmniNode-ai/omnimarket",
        current_phase=EnumFsmPhase(current_phase),
        consecutive_failures=consecutive_failures,
    )


async def _drive(
    command: ModelPrReviewAdvanceCommand, bus: Any, *, group: str
) -> list[Any]:
    """Publish one advance command over the bus; return output-topic history."""
    return await drive_round_trip(
        bus,
        handler=_PrReviewFsmBusHandler(),
        handler_name="pr-review-fsm-reducer",
        input_model_cls=None,
        start_topic=f"{_START_TOPIC}.{group}",
        output_topic=f"{_RESULT_TOPIC}.{group}",
        payload_bytes=command.model_dump_json().encode("utf-8"),
        group_id=group,
    )


async def _phase_after(
    command: ModelPrReviewAdvanceCommand, bus: Any, *, group: str
) -> dict[str, Any]:
    history = await _drive(command, bus, group=group)
    assert len(history) == 1, "expected exactly one FSM-state projection"
    projection: dict[str, Any] = json.loads(history[0].value)
    return projection


# ---------------------------------------------------------------------------
# Declared FSM state coverage — every phase entered, asserted by literal name.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
class TestPrReviewFsmDeclaredStateCoverage:
    @pytest.mark.parametrize(
        ("from_phase", "to_phase"),
        [
            ("init", "fetch_diff"),
            ("fetch_diff", "review"),
            ("review", "post_threads"),
            ("post_threads", "watch"),
            ("watch", "judge_verify"),
            ("judge_verify", "report"),
            ("report", "done"),
        ],
    )
    async def test_success_edge_enters_next_phase(
        self,
        integration_event_bus: Any,
        from_phase: str,
        to_phase: str,
    ) -> None:
        """Each declared success edge advances to the next declared phase."""
        command = ModelPrReviewAdvanceCommand(
            state=_state(from_phase), phase_success=True
        )
        projection = await _phase_after(
            command, integration_event_bus, group=f"pr-review-{from_phase}"
        )
        assert projection["current_phase"] == to_phase
        assert projection["consecutive_failures"] == 0
        assert projection["error_message"] is None

    async def test_init_state_holds_on_single_failure(
        self, integration_event_bus: Any
    ) -> None:
        """`init`: a single failure retries the phase in place (below the breaker)."""
        command = ModelPrReviewAdvanceCommand(
            state=_state("init"),
            phase_success=False,
            error_message="init phase blipped",
        )
        projection = await _phase_after(
            command, integration_event_bus, group="pr-review-init-retry"
        )
        assert projection["current_phase"] == "init"
        assert projection["consecutive_failures"] == 1
        assert projection["error_message"] == "init phase blipped"

    async def test_failed_terminal_state_via_circuit_breaker(
        self, integration_event_bus: Any
    ) -> None:
        """`failed`: the 3rd consecutive failure trips the circuit breaker."""
        # Two prior failures already recorded; this failing advance is the 3rd.
        command = ModelPrReviewAdvanceCommand(
            state=_state("fetch_diff", consecutive_failures=2),
            phase_success=False,
            error_message="third strike",
        )
        projection = await _phase_after(
            command, integration_event_bus, group="pr-review-breaker"
        )
        assert projection["current_phase"] == "failed"
        assert projection["consecutive_failures"] == 3
        assert projection["error_message"] == "third strike"


# ---------------------------------------------------------------------------
# REDUCER idempotency + negative control.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
class TestPrReviewFsmReducerDimensions:
    async def test_fold_is_deterministic(self, integration_event_bus: Any) -> None:
        """Folding the same advance twice yields the identical next phase."""
        cid = uuid4()
        command = ModelPrReviewAdvanceCommand(
            state=_state("review", correlation_id=cid), phase_success=True
        )
        first = await _phase_after(
            command, integration_event_bus, group="pr-review-det-1"
        )
        second = await _phase_after(
            command, integration_event_bus, group="pr-review-det-2"
        )
        assert first["current_phase"] == "post_threads"
        assert second["current_phase"] == first["current_phase"]

    @pytest.mark.parametrize("terminal_phase", ["done", "failed"])
    async def test_advance_from_terminal_publishes_no_projection(
        self, integration_event_bus: Any, terminal_phase: str
    ) -> None:
        """Negative control: advancing from a terminal phase raises in the handler,
        so the adapter publishes NO projection (empty output history)."""
        command = ModelPrReviewAdvanceCommand(
            state=_state(terminal_phase), phase_success=True
        )
        history = await _drive(
            command, integration_event_bus, group=f"pr-review-terminal-{terminal_phase}"
        )
        assert history == [], (
            f"advancing from terminal {terminal_phase!r} must publish nothing"
        )
