# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""PR-review FSM REDUCER handler (OMN-13212 / B2).

Re-expresses the deleted ``node_pr_review_bot`` ``HandlerPrReviewBot`` FSM as a
canonical REDUCER node. Pure fold: ``(state, phase-advance event) -> new state``.
It owns the phase sequence, the phase-transition event, and the 3-failure circuit
breaker. No I/O, no bus, no DB — the reducer only updates the FSM state
projection; the orchestrator publishes the transition events over the bus.

Dispatch:
  The runtime delivers a ``ModelEventEnvelope`` whose payload is a
  ``ModelPrReviewAdvanceCommand``. The handler returns
  ``ModelHandlerOutput.for_reducer`` carrying the advanced
  ``ModelPrReviewBotState`` as its projection.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import uuid4

from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

from omnimarket.nodes.node_pr_review_fsm_reducer.models.model_pr_review_advance_command import (
    ModelPrReviewAdvanceCommand,
)

# Re-exported so the golden-chain suite imports the pure fold helpers from the
# REDUCER handler (mirrors node_redeploy_fsm_reducer).
from omnimarket.review.pr_review_fsm import (
    ModelPhaseTransitionEvent,
    ModelPrReviewBotState,
    advance,
    make_verdict,
    start_state,
)

HANDLER_ID = "pr-review-fsm-reducer"


class HandlerPrReviewFsm:
    """Pure reducer: fold one phase-advance event into the FSM state projection."""

    async def handle(
        self, envelope: ModelEventEnvelope[Any]
    ) -> ModelHandlerOutput[None]:
        """Advance the FSM by one phase and emit the new state as a projection."""
        command = _coerce_command(envelope.payload)
        new_state, _event = advance(
            command.state,
            phase_success=command.phase_success,
            error_message=command.error_message,
            diff_hunks=command.diff_hunks,
            findings=command.findings,
            thread_states=command.thread_states,
        )
        return ModelHandlerOutput.for_reducer(
            input_envelope_id=envelope.envelope_id,
            correlation_id=(
                envelope.correlation_id or command.state.correlation_id or uuid4()
            ),
            handler_id=HANDLER_ID,
            projections=(new_state,),
        )


def _coerce_command(payload: Any) -> ModelPrReviewAdvanceCommand:
    """Coerce the dispatched payload into a ``ModelPrReviewAdvanceCommand``."""
    if isinstance(payload, ModelPrReviewAdvanceCommand):
        return payload
    if isinstance(payload, Mapping):
        return ModelPrReviewAdvanceCommand.model_validate(dict(payload))
    if hasattr(payload, "model_dump"):
        return ModelPrReviewAdvanceCommand.model_validate(payload.model_dump())
    raise TypeError(
        "pr-review FSM payload must be ModelPrReviewAdvanceCommand or a mapping; "
        f"got {type(payload).__name__}"
    )


__all__: list[str] = [
    "HANDLER_ID",
    "HandlerPrReviewFsm",
    "ModelPhaseTransitionEvent",
    "ModelPrReviewBotState",
    "advance",
    "make_verdict",
    "start_state",
]
