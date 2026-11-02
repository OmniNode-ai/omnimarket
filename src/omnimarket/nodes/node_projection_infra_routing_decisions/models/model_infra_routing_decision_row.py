# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One routing decision in the exact shape ``infra_routing_decisions`` stores."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelInfraRoutingDecisionRow(BaseModel):
    """The six columns a routing decision carries.

    Field names are the COLUMN names, not the wire names. The columns the
    decision does not carry (selection_mode, fallback_indicator, is_fallback,
    candidates_evaluated, candidate_providers, session_id, latency_ms) are
    absent on purpose: they take the defaults the table declares, because a
    plausible constant would be a fabricated fact in an evidence table.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID | None = Field(
        default=None,
        description="Correlation id of the delegation this decision routed.",
    )
    selected_provider: str = Field(
        description="Backend the decision selected (wire: selected_backend_ref).",
    )
    selected_tier: str = Field(
        description="Routing tier that produced the decision (wire: tier_name).",
    )
    selected_model: str = Field(description="Model the decision selected.")
    reason: str = Field(
        description="Why the decision selected that model (wire: rationale).",
    )
    task_type: str | None = Field(
        default=None,
        description="Task classification carried by the original request.",
    )


__all__: list[str] = ["ModelInfraRoutingDecisionRow"]
