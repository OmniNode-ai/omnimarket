# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Content-free candidate metadata supplied by the caller."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_delegation_eval_sample_compute.models.enum_delegation_eval_gate_outcome import (
    EnumDelegationEvalGateOutcome,
)


class ModelDelegationEvalCandidate(BaseModel):
    """Content-free candidate metadata supplied by the caller."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    attempt_index: int
    tenant_id: str
    task_class: str
    gate_outcome: EnumDelegationEvalGateOutcome
    deciding_path: str | None = None
