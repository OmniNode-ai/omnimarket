# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed reducer output for OmniGate projection updates."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_omnigate_projection.models.model_omnigate_projection_row import (
    ModelOmniGateMetricsSnapshot,
    ModelOmniGateProjectionRow,
)


class ModelOmniGateProjectionResult(BaseModel):
    """Activity rows and metrics snapshot after applying one event."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    activity: list[ModelOmniGateProjectionRow] = Field(default_factory=list)
    metrics: ModelOmniGateMetricsSnapshot = Field(
        default_factory=ModelOmniGateMetricsSnapshot
    )


__all__ = ["ModelOmniGateProjectionResult"]
