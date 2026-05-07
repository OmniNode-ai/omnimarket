# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Input model for the contract reducer node."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelReducerInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    current_state: dict[str, object] = Field(
        ..., description="Accumulated state; must contain 'current_step'."
    )
    user_response: str = Field(
        ..., description="The user's response token for the current step."
    )
    contract_transitions: list[dict[str, object]] = Field(
        ..., description="Transition table from the calling contract."
    )


__all__: list[str] = ["ModelReducerInput"]
