# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract parameters for deterministic bootstrap intervals (OMN-21132, plan task TC.9, port item AP-E1)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelRetrievalEvalIntervalMethod(BaseModel):
    """Contract parameters for deterministic bootstrap intervals."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    method: Literal["bootstrap_percentile"]
    confidence: float = Field(gt=0, lt=1)
    resamples: int = Field(ge=100)
    seed: int
