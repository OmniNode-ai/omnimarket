# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input of the landing reducer: the current row and one observation."""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.events.pr_landing.model_pr_landing_observation import (
    ModelPrLandingObservation,
)
from omnimarket.events.pr_landing.model_pr_landing_state import (
    ModelPrLandingState,
)


class ModelPrLandingReduceInput(BaseModel):
    """``state`` is None on first sight of a PR; otherwise it is the stored row."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: ModelPrLandingState | None = Field(
        default=None, description="The stored row, or None for a PR never seen."
    )
    observation: ModelPrLandingObservation = Field(...)

    @model_validator(mode="after")
    def _observation_is_for_this_row(self) -> Self:
        if self.state is not None and (
            self.state.landing_key != self.observation.landing_key
        ):
            msg = (
                f"observation for {self.observation.landing_key} cannot reduce "
                f"row {self.state.landing_key}"
            )
            raise ValueError(msg)
        return self


__all__: list[str] = ["ModelPrLandingReduceInput"]
