# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_swarm_endpoint_health_effect.models.enums import (
    EnumEndpointStatus,
    EnumModelStatus,
)


class EndpointHealth(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    endpoint_id: str
    endpoint_status: EnumEndpointStatus
    model_status: EnumModelStatus
    latency_ms: int | None = None
    error: str | None = None
    checked_at: str


class ModelSwarmHealthCheckResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    endpoint_health: dict[str, EndpointHealth]
    checked_at: str


__all__: list[str] = ["EndpointHealth", "ModelSwarmHealthCheckResult"]
