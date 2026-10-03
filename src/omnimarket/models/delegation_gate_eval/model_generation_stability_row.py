# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Group-level generation flip counts, rate, uncertainty and provenance."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.events.delegation_gate_eval.model_wilson_interval import (
    ModelWilsonInterval,
)


class ModelGenerationStabilityRow(BaseModel):
    """A class flips once per group with any disagreement among its verdicts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_class: str = Field(min_length=1)
    groups_n: int = Field(ge=1)
    flips_n: int = Field(ge=0)
    flip_rate: float = Field(ge=0.0, le=1.0)
    flip_rate_wilson: ModelWilsonInterval
    measured_backend_id: str = Field(min_length=1)
