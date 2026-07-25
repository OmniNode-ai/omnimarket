# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelLivenessDemandQueryRequest — query one surface's demand source (OMN-15126).

Carries the already-resolved `ModelLivenessRegistryEntry` (registry-entry
*resolution* — design §7 OPEN-3 — is the caller's/orchestrator's job, not
this EFFECT's; this node's sole responsibility is the demand-source query
and correlated-join proper, design §3.2 steps 2-3).
"""

from __future__ import annotations

from omnibase_core.models.runtime.model_liveness_registry_entry import (
    ModelLivenessRegistryEntry,
)
from pydantic import BaseModel, ConfigDict, Field

__all__ = ["ModelLivenessDemandQueryRequest"]


class ModelLivenessDemandQueryRequest(BaseModel):
    """Command to query one surface's declared demand source + output join."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    registry_entry: ModelLivenessRegistryEntry
    evaluation_window_limit: int = Field(
        default=500,
        ge=1,
        le=5000,
        description="Max eligible rows scanned this cycle (v1 hard cap; "
        "registry-declared ModelSamplingPolicy is not yet honored by this "
        "handler -- design §4 OPEN-8 -- a registry entry with a non-None "
        "sampling_policy fails closed rather than silently sampling).",
    )
