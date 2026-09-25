# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input to the dry-run board-truth reconciler.

There is deliberately no ``dry_run`` field. Dry-run is not a default a caller
can invert — the node has no write path at all. Arming live writes is OMN-16733,
which adds the path and the flag together, behind its own preconditions.

``extra="forbid"`` is what makes that structural: a caller passing ``dry_run=False``
gets a validation error rather than a silently ignored argument.

Related:
    - OMN-16729: Epic — board-truth mechanization
    - OMN-16732: this node
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.events.board_truth import (
    ModelBoardFactBundle,
)


class ModelBoardReconcileInput(BaseModel):
    """Board facts plus the ledger to merge claims from."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Reconciliation run correlation ID.")
    evaluated_at: datetime = Field(
        ..., description="Instant to evaluate the projection against."
    )
    staleness_days: int = Field(
        ...,
        description="How old activity may be before the reaper considers a claim stale.",
    )
    ledger_path: Path | None = Field(
        ...,
        description="Rolling work ledger to parse CLAIM/TERMINAL rows from; None to skip.",
    )
    tickets: tuple[ModelBoardFactBundle, ...] = Field(
        ...,
        description="Fact bundles from the board and PR adapters; ledger claims are merged in.",
    )


__all__: list[str] = ["ModelBoardReconcileInput"]
