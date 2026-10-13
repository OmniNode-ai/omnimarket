# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A fixed checklist scored over one delegated answer by another model (OMN-19787)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.delegation_gate_eval.model_checklist_item_score import (
    ModelChecklistItemScore,
)


class ModelChecklistScore(BaseModel):
    """Every item of the checklist, scored by ``scorer_model``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    checklist_id: str = Field(min_length=1)
    scorer_model: str = Field(min_length=1)
    items: tuple[ModelChecklistItemScore, ...] = Field(min_length=1)
