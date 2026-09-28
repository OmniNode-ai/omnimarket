# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure fold that reads the caller's lane off a delegate-skill terminal (OMN-19860).

Returns the delegation_events column for it. The effect writers persist what
it returns and decide nothing. A terminal with no lane yields no column, so a
laneless re-emit leaves a stored lane alone. A malformed lane is refused by
name and yields no column, so it never dead-letters the delegation row and is
never guessed into a lane.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.delegation.delegation_caller_lane import caller_lane_refusal
from omnimarket.models.delegation.wire.model_delegate_skill_terminal_projection import (
    ModelDelegateSkillTerminalProjection,
)


class ModelDelegationCallerLaneFold(BaseModel):
    """The outcome of folding a delegation's caller lane."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    caller_lane: str | None = Field(default=None)
    caller_lane_refusal: str | None = Field(default=None)

    @model_validator(mode="after")
    def _at_most_one_outcome(self) -> ModelDelegationCallerLaneFold:
        if self.caller_lane is not None and self.caller_lane_refusal is not None:
            raise ValueError(
                "a caller-lane fold holds exactly one of caller_lane and "
                "caller_lane_refusal"
            )
        return self

    def row_columns(self) -> dict[str, object]:
        """The delegation_events columns this fold names."""
        if self.caller_lane is not None:
            return {"caller_lane": self.caller_lane}
        return {}


class HandlerDelegationCallerLaneFold:
    """Fold a delegate-skill terminal to its caller-lane column."""

    def handle(
        self, request: ModelDelegateSkillTerminalProjection
    ) -> ModelDelegationCallerLaneFold:
        value = request.caller_lane
        if value is None:
            return ModelDelegationCallerLaneFold()
        refusal = caller_lane_refusal(value)
        if refusal is not None:
            return ModelDelegationCallerLaneFold(caller_lane_refusal=refusal)
        return ModelDelegationCallerLaneFold(caller_lane=value)


__all__ = ["HandlerDelegationCallerLaneFold", "ModelDelegationCallerLaneFold"]
