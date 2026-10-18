# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Deterministic retrieval evaluation report (OMN-21132, plan task TC.9, port item AP-E1)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.models.retrieval_eval.model_retrieval_eval_aggregate import (
    ModelRetrievalEvalAggregate,
)
from omnimarket.models.retrieval_eval.model_retrieval_eval_interval_method import (
    ModelRetrievalEvalIntervalMethod,
)
from omnimarket.models.retrieval_eval.model_retrieval_eval_query_score import (
    ModelRetrievalEvalQueryScore,
)
from omnimarket.models.retrieval_eval.model_retrieval_eval_target_verdict import (
    ModelRetrievalEvalTargetVerdict,
)


class ModelRetrievalEvalReport(BaseModel):
    """Deterministic retrieval evaluation report."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    eval_id: str
    metrics_version: str
    k_values: tuple[int, ...]
    interval: ModelRetrievalEvalIntervalMethod
    overall: ModelRetrievalEvalAggregate
    slices: tuple[ModelRetrievalEvalAggregate, ...]
    queries: tuple[ModelRetrievalEvalQueryScore, ...]
    target: ModelRetrievalEvalTargetVerdict
