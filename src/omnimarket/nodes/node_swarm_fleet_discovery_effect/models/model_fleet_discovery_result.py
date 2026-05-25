# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class EnumDiscoveryEndpointStatus(StrEnum):
    healthy = "healthy"
    unhealthy = "unhealthy"
    skipped = "skipped"


class ModelDiscoveredEndpoint(BaseModel):
    """A single endpoint discovered and health-checked during fleet discovery."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    base_url: str
    model_id: str
    provider: str
    capabilities: tuple[str, ...]
    context_window: int | None
    cost_basis: str
    status: EnumDiscoveryEndpointStatus
    latency_ms: int | None = None
    error: str | None = None


class ModelFleetDiscoveryResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    endpoints: tuple[ModelDiscoveredEndpoint, ...]
    healthy_count: int
    unhealthy_count: int
    local_count: int
    openrouter_count: int
    meets_threshold: bool
    discovered_at: str
    run_id: str = ""
    correlation_id: str = ""


__all__: list[str] = [
    "EnumDiscoveryEndpointStatus",
    "ModelDiscoveredEndpoint",
    "ModelFleetDiscoveryResult",
]
