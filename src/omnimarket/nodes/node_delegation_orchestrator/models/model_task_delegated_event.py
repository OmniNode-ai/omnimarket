# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2026 OmniNode Team
"""Backward-compatible task-delegated event for omnidash projection.

Maps delegation pipeline results to the existing TaskDelegatedEvent
schema consumed by omnidash's DelegationProjection via
onex.evt.omniclaude.task-delegated.v1.

Fields match the omnidash shared/delegation-types.ts TaskDelegatedEvent
interface and the omniclaude-projections.ts consumer expectations.
"""

from __future__ import annotations

from uuid import UUID

from omnibase_infra.enums.generated.enum_omniclaude_topic import EnumOmniclaudeTopic
from pydantic import BaseModel, ConfigDict, Field


class ModelTaskDelegatedEvent(BaseModel):
    """Backward-compatible event payload for omnidash delegation projection.

    This model matches the wire schema expected by omnidash's
    ReadModelConsumer for the task-delegated.v1 topic. Field names
    use snake_case to match the Kafka JSON wire format.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    topic: str = Field(
        default=EnumOmniclaudeTopic.EVT_TASK_DELEGATED_V1,
        description="Kafka topic this event should be routed to.",
    )
    timestamp: str = Field(..., description="ISO-8601 timestamp.")
    correlation_id: UUID = Field(..., description="Delegation correlation ID.")
    session_id: UUID | None = Field(default=None, description="Source session ID.")
    task_type: str = Field(..., description="Task classification.")
    delegated_to: str = Field(..., description="Model/endpoint that handled the task.")
    model_name: str = Field(
        default="",
        description="LLM model name from the routing decision (e.g. Qwen3-Coder-14B).",
    )
    delegated_by: str = Field(
        default="delegation-pipeline",
        description="Source of the delegation.",
    )
    quality_gate_passed: bool = Field(
        ..., description="Whether the quality gate accepted the response."
    )
    quality_gates_checked: list[str] = Field(
        default_factory=lambda: ["length", "refusal", "markers"],
        description="Names of quality gates checked.",
    )
    quality_gates_failed: list[str] = Field(
        default_factory=list,
        description="Names of quality gates that failed.",
    )
    cost_usd: float = Field(
        default=0.0, description="Estimated cost of local LLM inference (near-zero)."
    )
    cost_savings_usd: float = Field(
        default=0.0, description="Estimated savings vs Claude."
    )
    delegation_latency_ms: int = Field(
        default=0, description="End-to-end delegation latency in ms."
    )
    is_shadow: bool = Field(default=False, description="Whether this was a shadow run.")
    llm_call_id: str = Field(
        default="",
        description="Upstream LLM call ID for JOIN with llm_cost_aggregates.",
    )
    tokens_to_compliance: int = Field(
        default=0,
        ge=0,
        description="Total tokens across all compliance attempts.",
    )
    compliance_attempts: int = Field(
        default=1,
        ge=1,
        description="Number of LLM invocations to reach compliance.",
    )


__all__: list[str] = ["ModelTaskDelegatedEvent"]
