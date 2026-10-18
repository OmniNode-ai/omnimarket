# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Validated retrieval evaluation contract configuration (OMN-21132, plan task TC.9, port item AP-E1)."""

from __future__ import annotations

from itertools import pairwise
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.retrieval_eval.model_retrieval_eval_interval_method import (
    ModelRetrievalEvalIntervalMethod,
)
from omnimarket.models.retrieval_eval.model_retrieval_eval_target import (
    ModelRetrievalEvalTarget,
)


class ModelRetrievalEvalConfig(BaseModel):
    """Validated retrieval evaluation contract configuration."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    k_values: tuple[Annotated[int, Field(ge=1)], ...] = Field(min_length=1)
    target: ModelRetrievalEvalTarget
    interval: ModelRetrievalEvalIntervalMethod

    @model_validator(mode="after")
    def _validate_cutoffs(self) -> ModelRetrievalEvalConfig:
        if any(left >= right for left, right in pairwise(self.k_values)):
            raise ValueError("k_values must be strictly ascending without duplicates")
        if self.target.k is not None and self.target.k not in self.k_values:
            raise ValueError("target.k must be one of k_values")
        return self
