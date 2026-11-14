# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The answer to one client-call claim attempt (plan TB.3, OMN-21098)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_delegate_skill_orchestrator.models.enum_client_call_claim_action import (
    EnumClientCallClaimAction,
    EnumClientCallResumeStep,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.models.model_client_call_claim_row import (
    ModelClientCallClaimRow,
)


class ModelClientCallClaimDecision(BaseModel):
    """What the claim attempt did and what its caller does next.

    ``row`` is the row as the attempt left it (or found it, when the attempt
    wrote nothing). ``resume_step`` is set only on a takeover.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    action: EnumClientCallClaimAction
    row: ModelClientCallClaimRow
    resume_step: EnumClientCallResumeStep | None = None
