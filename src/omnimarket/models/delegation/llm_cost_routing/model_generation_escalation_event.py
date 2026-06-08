# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Event model for a generation-pipeline escalation being triggered (OMN-12829 / C1).

Emitted by node_generation_consumer when a generated node fails contract
validation and attempts remain. The next tier/model/endpoint is selected by the
ROUTING AUTHORITY (node_delegation_routing_reducer.delta) — generation never
picks the model itself. This event records exactly what the authority decided so
the escalation is provable.

Acceptance (C1): the escalation proof records tier, provider, model, endpoint,
attempt_count, escalation_reason.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelGenerationEscalationTriggeredEvent(BaseModel):
    """Generation escalation proof: the routing authority's escalated decision.

    Published to onex.evt.omnimarket.delegation-escalation-triggered.v1.

    Every field except ``escalation_reason`` is sourced from the routing
    authority's ``ModelRoutingDecision`` for the escalated tier — the generation
    consumer copies the decision verbatim and never selects a model itself.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    correlation_id: str = Field(
        ...,
        description="Echoed correlation_id of the generation run that escalated.",
    )
    task_type: str = Field(
        ...,
        description=(
            "Task class driving the escalation ladder (e.g. 'code_generation'); "
            "the routing authority resolves escalation_policy.tier_order from this."
        ),
    )
    tier: str = Field(
        ...,
        min_length=1,
        description=(
            "Routing tier the authority escalated to (e.g. 'cheap_cloud', "
            "'claude'). Sourced from the authority's ModelRoutingDecision.tier_name."
        ),
    )
    provider: str = Field(
        ...,
        min_length=1,
        description=(
            "Provider classification of the escalated tier ('local' for local "
            "tiers, 'cloud' otherwise). Derived from the authority's tier, not a "
            "code literal."
        ),
    )
    model: str = Field(
        ...,
        min_length=1,
        description="Model the routing authority selected for the escalated tier.",
    )
    endpoint: str = Field(
        ...,
        min_length=1,
        description=(
            "Complete endpoint URL the routing authority resolved for the "
            "escalated model, recorded verbatim."
        ),
    )
    attempt_count: int = Field(
        ...,
        ge=1,
        description="Attempt number that failed and triggered the escalation.",
    )
    escalation_reason: str = Field(
        ...,
        min_length=1,
        description=(
            "Why escalation fired — the contract-validation errors from the "
            "failed attempt."
        ),
    )


__all__ = ["ModelGenerationEscalationTriggeredEvent"]
