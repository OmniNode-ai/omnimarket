# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract quota for each stratum with this gate outcome."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_delegation_eval_sample_compute.models.enum_delegation_eval_gate_outcome import (
    EnumDelegationEvalGateOutcome,
)


class ModelDelegationEvalQuota(BaseModel):
    """Contract quota for each stratum with this gate outcome."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: EnumDelegationEvalGateOutcome
    count: int = Field(ge=0)
