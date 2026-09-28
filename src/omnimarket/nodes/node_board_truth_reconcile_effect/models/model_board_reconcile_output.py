# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Output of the dry-run board-truth reconciler: the projection plus its diff table.

Related:
    - OMN-16729: Epic — board-truth mechanization
    - OMN-16732: node_board_truth_reconcile_effect
    - OMN-16735: retires the manual sweep in favour of reading this report
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.events.board_truth import (
    ModelBoardTruthOutput,
)
from omnimarket.nodes.node_board_truth_reconcile_effect.models.model_board_reconcile_summary import (
    ModelBoardReconcileSummary,
)


class ModelBoardReconcileOutput(BaseModel):
    """The projection, the rendered diff table, and the headline counts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    projection: ModelBoardTruthOutput = Field(
        ..., description="Structured projection rows."
    )
    report: str = Field(..., description="Rendered diff table — this is the sweep.")
    summary: ModelBoardReconcileSummary = Field(..., description="Headline counts.")


__all__: list[str] = ["ModelBoardReconcileOutput"]
