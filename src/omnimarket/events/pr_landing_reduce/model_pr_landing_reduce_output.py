# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Output of the landing reducer: the next row, the edge taken and the intents."""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.events.pr_landing.model_pr_landing_intent import (
    ModelPrLandingIntent,
)
from omnimarket.events.pr_landing.model_pr_landing_state import (
    ModelPrLandingState,
)


class ModelPrLandingReduceOutput(BaseModel):
    """A dropped observation leaves the row unchanged and asks for nothing."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: ModelPrLandingState = Field(..., description="The row after this step.")
    trigger: str | None = Field(
        default=None,
        min_length=1,
        description="The contract state_machine trigger taken; None when dropped.",
    )
    intents: tuple[ModelPrLandingIntent, ...] = Field(default=())
    dropped_reason: str | None = Field(
        default=None,
        min_length=1,
        description=(
            "Why the observation moved nothing, e.g. a head other than the "
            "row's head_sha. None when a transition was taken."
        ),
    )

    @model_validator(mode="after")
    def _either_a_transition_or_a_drop(self) -> Self:
        dropped = self.dropped_reason is not None
        if dropped == (self.trigger is not None):
            msg = "exactly one of trigger and dropped_reason is set"
            raise ValueError(msg)
        if dropped and self.intents:
            msg = "a dropped observation asks for no intents"
            raise ValueError(msg)
        return self


__all__: list[str] = ["ModelPrLandingReduceOutput"]
