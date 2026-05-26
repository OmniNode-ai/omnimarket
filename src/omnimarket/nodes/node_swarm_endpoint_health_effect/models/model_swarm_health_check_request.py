# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_swarm_endpoint_health_effect.models.model_swarm_endpoint import (
    ModelSwarmEndpoint,
)


class ModelSwarmHealthCheckRequest(BaseModel):
    """Command/request accepted by the endpoint-health effect node.

    The orchestrator publishes ``endpoint_ids`` (string IDs only).  The
    handler resolves full ``ModelSwarmEndpoint`` objects from its registry
    contract when ``endpoints`` is not populated.  Direct callers (e.g.
    tests) may supply ``endpoints`` directly and omit ``endpoint_ids``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Full endpoint objects — populated by direct callers / tests.
    endpoints: tuple[ModelSwarmEndpoint, ...] = ()
    # String IDs — populated by the orchestrator.  Handler resolves these
    # against its registry when ``endpoints`` is empty.
    endpoint_ids: tuple[str, ...] = ()
    correlation_id: str
    run_id: str = ""


__all__: list[str] = ["ModelSwarmHealthCheckRequest"]
