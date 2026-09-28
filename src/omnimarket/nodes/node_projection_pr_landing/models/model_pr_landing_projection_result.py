# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The fold's output: the state upsert, and the transition append if any."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_projection_pr_landing.models.model_pr_landing_state_row import (
    ModelPrLandingStateRow,
)
from omnimarket.nodes.node_projection_pr_landing.models.model_pr_landing_transition_row import (
    ModelPrLandingTransitionRow,
)


class ModelPrLandingProjectionResult(BaseModel):
    """Every event writes the state row; only a transition appends to the log."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state_row: ModelPrLandingStateRow = Field(...)
    transition_row: ModelPrLandingTransitionRow | None = Field(default=None)


__all__: list[str] = ["ModelPrLandingProjectionResult"]
