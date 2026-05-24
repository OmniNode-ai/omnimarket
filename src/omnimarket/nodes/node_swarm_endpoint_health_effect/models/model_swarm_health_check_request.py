# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_swarm_endpoint_health_effect.models.model_swarm_endpoint import (
    ModelSwarmEndpoint,
)


class ModelSwarmHealthCheckRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    endpoints: tuple[ModelSwarmEndpoint, ...]
    correlation_id: str


__all__: list[str] = ["ModelSwarmHealthCheckRequest"]
