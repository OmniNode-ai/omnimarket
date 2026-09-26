# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Headline counts for one reconciliation run.

Related:
    - OMN-16729: Epic — board-truth mechanization
    - OMN-16732: node_board_truth_reconcile_effect
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelBoardReconcileSummary(BaseModel):
    """What the run found, at a glance."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evaluated_count: int = Field(..., description="Tickets evaluated.")
    no_change_count: int = Field(
        ..., description="Rows where the board already agrees."
    )
    flip_count: int = Field(
        ..., description="Rows where the facts entail a different state."
    )
    discrepancy_count: int = Field(
        ..., description="Rows reported but deliberately not actionable."
    )
    requires_confirmation_count: int = Field(
        ...,
        description="FLIP rows that would overwrite a human-set or unattributed state.",
    )
    ledger_claims_parsed: int = Field(
        ..., description="CLAIM/TERMINAL rows successfully bound to a ticket."
    )


__all__: list[str] = ["ModelBoardReconcileSummary"]
