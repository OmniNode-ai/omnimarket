# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelFleetDiscoveryRequest(BaseModel):
    """Command accepted by the fleet discovery effect node.

    Probes OpenRouter /api/v1/models for available free-tier models and
    health-checks local endpoints from the registry contract.  Returns a
    unified list of registered healthy endpoints for the swarm orchestrator.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    run_id: str = ""
    # When True, include local endpoints from the registry contract.
    include_local: bool = True
    # When True, probe OpenRouter and include cloud free-tier endpoints.
    include_openrouter: bool = True
    # Minimum healthy endpoints required; discovery fails if below threshold.
    min_healthy_endpoints: int = 8


__all__: list[str] = ["ModelFleetDiscoveryRequest"]
