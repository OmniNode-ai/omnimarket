# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and result of the declared-transition decision.

Both delegation paths, the bus orchestrator and the in-process local port, hand
the escalation decision node one of these after a rung fails or is skipped. The
node reads the transition from the task class's declared lists and returns it;
the paths only classify what the rung did into a cause and resolve the next rung
the decision asks for.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.delegation.model_escalation_transitions import (
    EnumEscalationCause,
    EnumMoveKind,
    EnumTransitionKind,
    ModelTaskClassTransitions,
)


class ModelTransitionDecisionRequest(BaseModel):
    """What one rung did, and the run state the declared transition depends on."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    cause: EnumEscalationCause
    transitions: ModelTaskClassTransitions
    pinned: bool = Field(
        default=False,
        description="The caller pinned the run to one rung; it never moves.",
    )
    retries_on_rung: int = Field(default=0, ge=0)
    rung_retry_budget: int = Field(
        default=0,
        ge=0,
        description="Retries the current rung declares for a retry_on cause.",
    )
    escalation_count: int = Field(default=0, ge=0)
    max_escalations: int | None = Field(
        default=None,
        ge=0,
        description="Bound on tier climbs. ``None`` means no bound applies.",
    )
    move_kind: EnumMoveKind = Field(
        default=EnumMoveKind.TIER_CLIMB,
        description="The kind of move the path would take next.",
    )
    next_rung_available: bool | None = Field(
        default=None,
        description=(
            "Whether the path found a next rung for ``move_kind``. ``None`` "
            "means it has not looked yet: a move is returned pending that lookup."
        ),
    )


class ModelTransitionDecision(BaseModel):
    """The declared transition for one rung, with the cause that decided it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: EnumTransitionKind
    cause: EnumEscalationCause = Field(
        description=(
            "The cause the transition was read for. It differs from the "
            "request's when a spent retry budget, a pin, the escalation limit or "
            "an empty ladder turned it into another declared cause."
        ),
    )
    move_kind: EnumMoveKind | None = None


__all__ = ["ModelTransitionDecision", "ModelTransitionDecisionRequest"]
