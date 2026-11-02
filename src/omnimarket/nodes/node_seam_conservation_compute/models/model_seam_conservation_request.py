# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request of node_seam_conservation_compute: one seam, one finalized window, both sides' counts."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.seam_reading import (
    ModelSeamCount,
    ModelSeamDeclaration,
    ModelSeamWindow,
    ModelSeamWindowClose,
)


class ModelSeamConservationRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    declaration: ModelSeamDeclaration
    window: ModelSeamWindow
    tick_id: str = Field(min_length=1)
    sender_counts: tuple[ModelSeamCount, ...] = ()
    receiver_counts: tuple[ModelSeamCount, ...] = ()
    sender_close: ModelSeamWindowClose | None = None
    receiver_close: ModelSeamWindowClose | None = None
    latency_budget_seconds: float | None = Field(
        default=None,
        ge=0,
        description="The seam's budget, read by the caller from the view_seams overlay key the declaration names.",
    )
    read_at: datetime
