# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Overall or slice metrics with failed queries in every denominator (OMN-21132, plan task TC.9, port item AP-E1)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.retrieval_eval.model_retrieval_metric_summary import (
    ModelRetrievalMetricSummary,
)


class ModelRetrievalEvalAggregate(BaseModel):
    """Overall or slice metrics with failed queries in every denominator."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    slice_label: str
    query_count: int = Field(ge=1)
    failed_count: int = Field(ge=0)
    metrics: tuple[ModelRetrievalMetricSummary, ...]
