# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Validated immutable sampling policy loaded before handler execution."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.nodes.node_delegation_eval_sample_compute.models.enum_delegation_eval_gate_outcome import (
    EnumDelegationEvalGateOutcome,
)
from omnimarket.nodes.node_delegation_eval_sample_compute.models.model_delegation_eval_quota import (
    ModelDelegationEvalQuota,
)


class ModelDelegationEvalSamplingConfig(BaseModel):
    """Validated immutable sampling policy loaded before handler execution."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    quotas: tuple[ModelDelegationEvalQuota, ...]
    holdout_buckets: int = Field(gt=0)
    reserved_bucket: int = Field(ge=0)
    order_key: Literal["sha256(seed + correlation_id + attempt_index)"]

    @model_validator(mode="after")
    def validate_policy(self) -> Self:
        if self.reserved_bucket >= self.holdout_buckets:
            raise ValueError("reserved_bucket must be less than holdout_buckets")
        names = [row.name for row in self.quotas]
        if len(names) != len(set(names)) or set(names) != set(
            EnumDelegationEvalGateOutcome
        ):
            raise ValueError("quotas must contain each gate outcome exactly once")
        return self
