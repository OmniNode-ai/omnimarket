# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Advance command for the redeploy FSM reducer.

The reducer folds one phase-advance event into the FSM state projection. The
prior state, the phase outcome, and an optional error message are the only inputs
the pure transition needs.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.events.runtime_deployment import ModelRedeployState


class ModelRedeployAdvanceCommand(BaseModel):
    """One phase-advance event folded into the redeploy FSM state."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: ModelRedeployState = Field(
        ..., description="Prior FSM state to advance from."
    )
    phase_success: bool = Field(
        ..., description="True if the just-completed phase succeeded."
    )
    error_message: str | None = Field(
        default=None, description="Error from the failing phase, if any."
    )


__all__: list[str] = ["ModelRedeployAdvanceCommand"]
