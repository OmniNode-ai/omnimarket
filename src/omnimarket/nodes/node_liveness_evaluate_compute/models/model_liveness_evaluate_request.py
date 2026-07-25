# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelLivenessEvaluateRequest — input to the pure liveness state decision.

OMN-15126 implementation of the OMN-14845 design (design §3.2). This model
carries the *already-computed* outcome of registry resolution (step 1) and
the demand-source query + correlated join (steps 2-3, performed by
node_liveness_demand_query_effect) so the compute handler can make ONLY the
state decision -- no I/O, deterministic given this input.
"""

from __future__ import annotations

from datetime import datetime
from typing import Self
from uuid import UUID

from omnibase_core.models.runtime.model_event_ref import ModelEventRef
from omnibase_core.models.runtime.model_sampling_policy import ModelSamplingPolicy
from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = ["ModelLivenessEvaluateRequest"]


class ModelLivenessEvaluateRequest(BaseModel):
    """Input to `HandlerLivenessEvaluateCompute` -- one evaluation cycle."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    surface_id: str = Field(..., min_length=1)
    lane: str = Field(..., min_length=1)
    deployed_sha: str = Field(..., min_length=1)
    image_digest: str = Field(..., min_length=1)
    config_digest: str = Field(..., min_length=1)
    runner: str = Field(..., min_length=1)
    independent_verifier: str | None = Field(default=None)
    evaluated_at: datetime
    freshness_window_seconds: int = Field(..., ge=1)
    error_budget_ratio: float = Field(..., ge=0.0, le=1.0)

    # Step 1 (registry resolution) and step 2 (demand-source query) outcomes.
    # Both are I/O performed upstream; this model only carries their result.
    registry_resolved: bool = Field(default=True)
    demand_query_succeeded: bool = Field(default=True)
    not_ready_reason: str | None = Field(default=None)

    # Step 3 (correlated join) evidence, from node_liveness_demand_query_effect.
    eligible_count: int = Field(default=0, ge=0)
    checked_count: int = Field(default=0, ge=0)
    failed_count: int = Field(default=0, ge=0)
    demand_query_evidence: str | None = Field(default=None)

    correlation_id: UUID | None = Field(default=None)
    input_event_ref: ModelEventRef | None = Field(default=None)
    terminal_event_ref: ModelEventRef | None = Field(default=None)
    projection_key_canonical: str | None = Field(default=None)
    projection_value_hash: str | None = Field(default=None)
    projection_expected_value_hash: str | None = Field(default=None)
    expected_value_predicate_result: bool | None = Field(default=None)
    failure_detail: str | None = Field(default=None)
    sampling_applied: ModelSamplingPolicy | None = Field(default=None)
    demand_synthetic: bool = Field(default=False)

    # Step 4 (freshness) input: the surface's most recent completed HEALTHY
    # receipt, if any. Both fields must be provided together or both omitted.
    prior_healthy_receipt_id: UUID | None = Field(default=None)
    prior_healthy_at: datetime | None = Field(default=None)

    @model_validator(mode="after")
    def _validate_prior_healthy_pairing(self) -> Self:
        has_id = self.prior_healthy_receipt_id is not None
        has_at = self.prior_healthy_at is not None
        if has_id != has_at:
            raise ValueError(  # error-ok: intentional request-shape rejection
                "prior_healthy_receipt_id and prior_healthy_at must be "
                "provided together (or both omitted); got "
                f"prior_healthy_receipt_id={self.prior_healthy_receipt_id!r} "
                f"prior_healthy_at={self.prior_healthy_at!r}."
            )
        return self
