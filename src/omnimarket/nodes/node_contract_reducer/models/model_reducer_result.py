# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Output model for the contract reducer node."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelReducerResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    updated_state: dict[str, object] = Field(
        ..., description="State after applying the transition."
    )
    next_step: str = Field(..., description="Step identifier the flow advances to.")
    is_terminal: bool = Field(
        ..., description="True when next_step is 'done' or has no outgoing transition."
    )


__all__: list[str] = ["ModelReducerResult"]
