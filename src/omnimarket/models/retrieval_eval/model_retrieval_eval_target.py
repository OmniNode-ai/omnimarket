# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The contract target applied to the overall query mean (OMN-21132, plan task TC.9, port item AP-E1)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.retrieval_eval.enum_retrieval_metric import EnumRetrievalMetric


class ModelRetrievalEvalTarget(BaseModel):
    """The contract target applied to the overall query mean."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    metric: EnumRetrievalMetric
    k: int | None = Field(ge=1)
    min_value: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def _validate_cutoff(self) -> ModelRetrievalEvalTarget:
        if self.metric in (
            EnumRetrievalMetric.RECALL_AT_K,
            EnumRetrievalMetric.PRECISION_AT_K,
        ):
            if self.k is None:
                raise ValueError("k is required for recall_at_k and precision_at_k")
        elif self.k is not None:
            raise ValueError("k must be None for average_precision and reciprocal_rank")
        return self
