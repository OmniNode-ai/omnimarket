# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 OmniNode Team
"""Typed DTOs for the invocation-only ADK adapter (OMN-13611, WS-C Phase 2.1).

These models are adapter-private. They carry the contract/overlay-resolved
invocation transport binding and the dispatch envelope the adapter hands to the
canonical remote-agent invoke surface. None of them encode routing/selection
state — they describe an *already-routed* invocation.
"""

from __future__ import annotations

from uuid import UUID

from omnibase_core.enums.enum_agent_protocol import EnumAgentProtocol
from omnibase_core.models.common.model_schema_value import ModelSchemaValue
from pydantic import BaseModel, ConfigDict, Field


class ModelAdkRunnerBinding(BaseModel):
    """Contract/overlay-resolved ADK runner transport binding.

    ``credential_secret_ref`` is a *reference* into the secret store; the literal
    secret value is resolved at the effect boundary, never embedded here and
    never read from ``os.environ`` by the adapter.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    credential_secret_ref: str = Field(
        ...,
        min_length=1,
        description=(
            "Secret-store reference for the ADK credential. Resolved to a literal "
            "value only at the effect boundary; never embedded or env-sourced here."
        ),
    )
    endpoint_url: str = Field(
        ...,
        min_length=1,
        description=(
            "ADK transport endpoint base, resolved from contract deep-merged with "
            "the overlay file. Never an environment variable (OMN-12803)."
        ),
    )


class ModelAdkInvokeConfig(BaseModel):
    """Contract/overlay-resolved configuration for the invocation-only ADK adapter.

    This is a transport binding, not a routing table. It records which agent
    protocol the adapter binds and the ADK runner binding (the canonical invoke
    topic is owned by the orchestrator contract, not this config). It contains no tier ladder, no model-selection list, and no
    escalation policy — those remain owned by the routing authority.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    config_version: str = Field(
        ..., min_length=1, description="Semantic version of the adk_invoke config."
    )
    agent_protocol: EnumAgentProtocol = Field(
        ...,
        description="Agent-level protocol this adapter binds. Must be AGENT-kind.",
    )
    adk_runner: ModelAdkRunnerBinding = Field(
        ..., description="Resolved ADK runner transport binding."
    )


class ModelAdkInvocationDispatch(BaseModel):
    """The dispatch envelope the ADK adapter hands to the canonical invoke surface.

    Built from an *already-routed* ``ModelInvocationCommand``. The adapter copies
    the routing authority's resolved fields verbatim (``target_ref``,
    ``agent_protocol``, ``payload``, correlation ids) and attaches the resolved
    transport binding. It performs no provider/model/tier/escalation selection.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    invoke_topic: str = Field(
        ..., min_length=1, description="Canonical remote-agent invoke topic."
    )
    task_id: UUID = Field(..., description="Task id copied from the routed command.")
    correlation_id: UUID = Field(
        ..., description="Correlation id copied from the routed command."
    )
    agent_protocol: EnumAgentProtocol = Field(
        ..., description="Agent protocol copied from the routed command."
    )
    target_ref: str = Field(
        ...,
        min_length=1,
        description="Target agent reference resolved by the routing authority.",
    )
    credential_secret_ref: str = Field(
        ...,
        min_length=1,
        description="Secret-store reference for the ADK credential (not a value).",
    )
    endpoint_url: str = Field(
        ..., min_length=1, description="ADK transport endpoint base from config."
    )
    payload: dict[str, ModelSchemaValue] = Field(
        default_factory=dict,
        description="Invocation payload copied verbatim from the routed command.",
    )


__all__ = [
    "ModelAdkInvocationDispatch",
    "ModelAdkInvokeConfig",
    "ModelAdkRunnerBinding",
]
