# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A query mean and its deterministic percentile interval (OMN-21132, plan task TC.9, port item AP-E1)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.retrieval_eval.enum_retrieval_metric import EnumRetrievalMetric


class ModelRetrievalMetricSummary(BaseModel):
    """A query mean and its deterministic percentile interval."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    metric: EnumRetrievalMetric
    k: int | None
    mean: float = Field(ge=0, le=1)
    interval_low: float = Field(ge=0, le=1)
    interval_high: float = Field(ge=0, le=1)
