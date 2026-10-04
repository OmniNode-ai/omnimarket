# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Generation stability measurements for the nonempty classes in a batch."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.models.delegation_gate_eval.model_generation_stability_row import (
    ModelGenerationStabilityRow,
)


class ModelGenerationStabilityResult(BaseModel):
    """Per-class measurements, sorted by class; absent classes have no row."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rows: tuple[ModelGenerationStabilityRow, ...] = ()
