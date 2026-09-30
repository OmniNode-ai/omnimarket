# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""JSON export shape: rows from omninode_internal.pr_state."""

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_projection_pr_state.models.model_pr_state_parity_row import (
    ModelPrStateParityRow,
)


class ModelPrStateParityExport(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    rows: tuple[ModelPrStateParityRow, ...]
