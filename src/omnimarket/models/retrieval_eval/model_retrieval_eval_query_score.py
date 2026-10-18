# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Metrics for one query, including failed queries (OMN-21132, plan task TC.9, port item AP-E1)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.retrieval_eval.model_retrieval_metric_at_k import (
    ModelRetrievalMetricAtK,
)


class ModelRetrievalEvalQueryScore(BaseModel):
    """Metrics for one query, including failed queries."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    query_id: str = Field(min_length=1)
    failed: bool
    relevant_count: int = Field(ge=1)
    retrieved_count: int = Field(ge=0)
    recall_at_k: tuple[ModelRetrievalMetricAtK, ...]
    precision_at_k: tuple[ModelRetrievalMetricAtK, ...]
    average_precision: float = Field(ge=0, le=1)
    reciprocal_rank: float = Field(ge=0, le=1)
