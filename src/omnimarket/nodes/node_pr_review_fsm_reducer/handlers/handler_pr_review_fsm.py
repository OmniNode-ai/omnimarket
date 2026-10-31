# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""PR-review FSM REDUCER handler (OMN-13212 / B2).

Re-expresses the deleted ``node_pr_review_bot`` ``HandlerPrReviewBot`` FSM as a
canonical REDUCER node. Pure fold: ``(state, phase-advance event) -> new state``.
It owns the phase sequence, the phase-transition event, and the 3-failure circuit
breaker. No I/O, no bus, no DB — the reducer only updates the FSM state
projection; the orchestrator publishes the transition events over the bus.

Dispatch:
  The runtime adapter unwraps the event and hands the handler the typed
  ``ModelPrReviewAdvanceCommand``. The handler returns
  ``ModelHandlerOutput.for_reducer`` carrying the advanced
  ``ModelPrReviewBotState`` as its projection.
"""

from __future__ import annotations

from uuid import uuid4

from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput

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
        self, request: ModelPrReviewAdvanceCommand
    ) -> ModelHandlerOutput[None]:
        """Advance the FSM by one phase and emit the new state as a projection."""
        new_state, _event = advance(
            request.state,
            phase_success=request.phase_success,
            error_message=request.error_message,
            diff_hunks=request.diff_hunks,
            findings=request.findings,
            thread_states=request.thread_states,
        )
        return ModelHandlerOutput.for_reducer(
            input_envelope_id=uuid4(),
            correlation_id=request.state.correlation_id or uuid4(),
            handler_id=HANDLER_ID,
            projections=(new_state,),
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
