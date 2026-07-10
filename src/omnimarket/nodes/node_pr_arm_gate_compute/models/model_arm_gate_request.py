# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelArmGateRequest — top-level input to node_pr_arm_gate_compute."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_pr_arm_gate_compute.models.model_arm_candidate import (
    ModelArmCandidate,
)
from omnimarket.nodes.node_pr_arm_gate_compute.models.model_arm_gate_policy import (
    ModelArmGatePolicy,
)


class ModelArmGateRequest(BaseModel):
    """One candidate PR plus the policy it is evaluated against."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate: ModelArmCandidate = Field(..., description="Genuine per-PR facts.")
    policy: ModelArmGatePolicy = Field(
        ..., description="Operator-controlled action policy."
    )


__all__: list[str] = ["ModelArmGateRequest"]
