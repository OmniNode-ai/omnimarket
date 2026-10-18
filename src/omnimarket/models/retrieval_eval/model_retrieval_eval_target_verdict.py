# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The observed overall target mean and its verdict (OMN-21132, plan task TC.9, port item AP-E1)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.retrieval_eval.enum_retrieval_target_verdict import (
    EnumRetrievalTargetVerdict,
)
from omnimarket.models.retrieval_eval.model_retrieval_eval_target import (
    ModelRetrievalEvalTarget,
)


class ModelRetrievalEvalTargetVerdict(BaseModel):
    """The observed overall target mean and its verdict."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    target: ModelRetrievalEvalTarget
    observed_mean: float = Field(ge=0, le=1)
    verdict: EnumRetrievalTargetVerdict
