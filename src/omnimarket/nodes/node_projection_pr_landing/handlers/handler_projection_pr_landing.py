# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The pure fold over one landing event (OMN-19833).

Rule 7a definition-B: ``handle(request) -> result``. No clock, no broker, no
database. Every time the rows carry is a field of the event, so the same event
always folds to the same rows and a replay is byte-identical.

This class is half of the pair. The writer beside it is the entry the runtime
calls; a pure entry alone on a projection arm validates, returns and stores
nothing while every watermark reads healthy (rule 7a, OMN-18769).
"""

from __future__ import annotations

from omnimarket.events.pr_landing.enum_pr_landing_state import (
    EnumPrLandingState,
)
from omnimarket.nodes.node_projection_pr_landing.models import (
    EnumPrLandingProjectionEventKind,
    ModelPrLandingProjectionRequest,
    ModelPrLandingProjectionResult,
    ModelPrLandingStateRow,
    ModelPrLandingTransitionRow,
)


class HandlerProjectionPrLanding:
    """Folds one transitioned, agent-needed, merged or closed event into rows."""

    def handle(
        self, request: ModelPrLandingProjectionRequest
    ) -> ModelPrLandingProjectionResult:
        if request.transitioned is not None:
            event = request.transitioned
            # The reopen rows of the table (F10, G5): out of CLOSED into any
            # other state, including CLOSED -> MERGED in a new episode. A newer
            # closed while CLOSED stays in the episode.
            opens_episode = (
                event.from_state is EnumPrLandingState.CLOSED
                and event.to_state is not EnumPrLandingState.CLOSED
            )
            transition = ModelPrLandingTransitionRow(
                repository=event.repository,
                pr_number=event.pr_number,
                seq=event.seq,
                head_sha=event.head_sha,
                from_state=event.from_state,
                to_state=event.to_state,
                trigger=event.trigger,
                intents=event.intents,
                opens_episode=opens_episode,
                transitioned_at=event.transitioned_at,
            )
            state = ModelPrLandingStateRow(
                repository=event.repository,
                pr_number=event.pr_number,
                seq=event.seq,
                event_kind=EnumPrLandingProjectionEventKind.TRANSITIONED,
                state=event.to_state,
                head_sha=event.head_sha,
                trigger=event.trigger,
                opens_episode=opens_episode,
                event_at=event.transitioned_at,
            )
            return ModelPrLandingProjectionResult(
                state_row=state, transition_row=transition
            )

        if request.agent_needed is not None:
            needed = request.agent_needed
            return ModelPrLandingProjectionResult(
                state_row=ModelPrLandingStateRow(
                    repository=needed.repository,
                    pr_number=needed.pr_number,
                    seq=needed.seq,
                    event_kind=EnumPrLandingProjectionEventKind.AGENT_NEEDED,
                    state=EnumPrLandingState.NEEDS_AGENT,
                    head_sha=needed.head_sha,
                    agent_reason=needed.reason,
                    agent_detail=needed.detail,
                    event_at=needed.raised_at,
                )
            )

        if request.merged is not None:
            merged = request.merged
            return ModelPrLandingProjectionResult(
                state_row=ModelPrLandingStateRow(
                    repository=merged.repository,
                    pr_number=merged.pr_number,
                    seq=merged.seq,
                    event_kind=EnumPrLandingProjectionEventKind.MERGED,
                    state=EnumPrLandingState.MERGED,
                    head_sha=merged.head_sha,
                    episode=merged.episode,
                    terminal_at=merged.merged_at,
                    event_at=merged.merged_at,
                )
            )

        closed = request.closed
        # The request validator guarantees exactly one event is present.
        assert closed is not None
        return ModelPrLandingProjectionResult(
            state_row=ModelPrLandingStateRow(
                repository=closed.repository,
                pr_number=closed.pr_number,
                seq=closed.seq,
                event_kind=EnumPrLandingProjectionEventKind.CLOSED,
                state=EnumPrLandingState.CLOSED,
                head_sha=closed.head_sha,
                episode=closed.episode,
                terminal_at=closed.closed_at,
                event_at=closed.closed_at,
            )
        )


__all__ = ["HandlerProjectionPrLanding"]
