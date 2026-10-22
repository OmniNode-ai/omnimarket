# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Wire models for one measured model selection (OMN-21207)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.delegation.model_measured_selection import (
    ModelMeasuredSelection,
    ModelPricedModel,
    ModelRecordedRun,
    ModelSelectionCandidate,
)


class ModelMeasuredSelectionRequest(BaseModel):
    """Rank these candidates on these prices and these recorded runs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    task_type: str = Field(min_length=1)
    #: The routing contract's candidates, each with its measure sources.
    candidates: tuple[ModelSelectionCandidate, ...] = Field(min_length=1)
    pricing: tuple[ModelPricedModel, ...]
    runs: tuple[ModelRecordedRun, ...]
    decided_at: datetime


class ModelMeasuredSelectionDecidedEvent(BaseModel):
    """The delegation decision on measures, with every value it read.

    Each value carries its captured_at, so the choice can be re-derived later
    from the same prices and runs.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    task_type: str
    selection: ModelMeasuredSelection


__all__ = ["ModelMeasuredSelectionDecidedEvent", "ModelMeasuredSelectionRequest"]
