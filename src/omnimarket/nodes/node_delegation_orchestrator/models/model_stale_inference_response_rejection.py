# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2026 OmniNode Team
"""Typed evidence for a rejected, superseded inference response (OMN-15542).

When the delegation orchestrator drops a response because it was produced by an
inference attempt the workflow has already left, the drop must not be silent. A
log line is not evidence: it is not typed, not durable, and not readable from
the control plane. This model is the durable record of the rejection, appended
to ``DelegationWorkflowState.stale_response_rejections`` and persisted with the
rest of the workflow state through the node's ``state_io`` codec.

It records BOTH sides of the identity mismatch and the route the response would
otherwise have been relabelled onto — which is the whole point. The live defect
(2026-07-30 ``.201`` 13-class matrix, correlation ``7a300730-...011``) was not
that a response was lost; it was that a superseded local response was ACCEPTED
and stamped with the succeeding cloud route's endpoint and tier, producing an
internally impossible provenance triple. The counterfactual fields below say
exactly which relabelling this rejection prevented.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelStaleInferenceResponseRejection(BaseModel):
    """Evidence record for one inference response dropped as superseded."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(
        ...,
        description="Workflow correlation the stale response addressed. Shared by every attempt, which is why it cannot bind a response to a route.",
    )
    rejected_attempt_id: UUID = Field(
        ...,
        description="Inference-attempt identity the rejected response carried — a superseded attempt.",
    )
    current_attempt_id: UUID = Field(
        ...,
        description="Inference-attempt identity actually in flight when the stale response arrived.",
    )
    response_model_used: str = Field(
        ...,
        description="model_used the rejected response reported. Truthful for its own attempt; incoherent against the current route.",
    )
    response_was_error: bool = Field(
        ...,
        description="Whether the rejected response was an error. A late error from a superseded attempt would otherwise have escalated the ladder a second time.",
    )
    current_tier_name: str | None = Field(
        default=None,
        description="Tier the workflow had moved on to — the tier this response would have been relabelled onto.",
    )
    current_endpoint_url: str | None = Field(
        default=None,
        description="Endpoint of the live route — the provider this response would have been falsely attributed to.",
    )
    current_selected_model: str | None = Field(
        default=None,
        description="Model the live route selected. Pair it with response_model_used to read the provenance mismatch that was prevented.",
    )
    rejected_at: datetime = Field(
        ..., description="When the orchestrator rejected the response."
    )


__all__: list[str] = ["ModelStaleInferenceResponseRejection"]
