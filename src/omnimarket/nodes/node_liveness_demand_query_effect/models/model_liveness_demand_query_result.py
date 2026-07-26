# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelLivenessDemandQueryResult — raw demand-query + join evidence (OMN-15126).

This is the EFFECT's output: everything node_liveness_evaluate_compute needs
to make the state decision (design §3.2 steps 3-4) without performing any I/O
of its own. `query_succeeded=False` covers both "registry-declared demand
source kind unsupported" and "the query itself errored" -- both are the
design's step-2 NOT_READY case (design §3.2: "query itself errors, times
out, or returns an incomplete/partial result").
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_liveness_demand_query_effect.models.model_liveness_join_sample import (
    ModelLivenessJoinSample,
)

__all__ = ["ModelLivenessDemandQueryResult"]


class ModelLivenessDemandQueryResult(BaseModel):
    """Raw demand-query + correlated-join evidence for one evaluation cycle."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    surface_id: str = Field(..., min_length=1)
    query_succeeded: bool
    error_message: str | None = Field(default=None)

    eligible_count: int = Field(default=0, ge=0)
    checked_count: int = Field(default=0, ge=0)
    failed_count: int = Field(default=0, ge=0)
    demand_query_evidence: str | None = Field(
        default=None,
        description="Proof the query ran, e.g. table + predicate + row count "
        "(design §5 demand_query_evidence).",
    )

    healthy_sample: ModelLivenessJoinSample | None = Field(
        default=None,
        description="First eligible item whose correlated join succeeded, "
        "if any (feeds a HEALTHY receipt).",
    )
    failed_sample: ModelLivenessJoinSample | None = Field(
        default=None,
        description="First eligible item whose correlated join failed, if "
        "any (feeds a RED receipt's None-with-reason evidence).",
    )
