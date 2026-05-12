# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2026 OmniNode Team
"""Routing intent model emitted by the delegation orchestrator."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)


class ModelRoutingIntent(BaseModel):
    """Intent emitted when the orchestrator requests routing."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    intent: str = Field(default="routing_reducer")
    payload: ModelDelegationRequest


__all__: list[str] = ["ModelRoutingIntent"]
