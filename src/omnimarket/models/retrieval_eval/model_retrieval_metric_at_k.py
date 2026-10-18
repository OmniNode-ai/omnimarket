# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A metric value at a positive retrieval cutoff (OMN-21132, plan task TC.9, port item AP-E1)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelRetrievalMetricAtK(BaseModel):
    """A metric value at a positive retrieval cutoff."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    k: int = Field(ge=1)
    value: float = Field(ge=0, le=1)
