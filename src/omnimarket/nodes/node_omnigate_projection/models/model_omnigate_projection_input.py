# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Typed reducer input for OmniGate projection updates."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_omnigate_projection.models.model_omnigate_projection_row import (
    ModelOmniGateMetricsSnapshot,
    ModelOmniGateProjectionRow,
)


class ModelOmniGateProjectionInput(BaseModel):
    """Envelope consumed by the OmniGate projection reducer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    activity: list[ModelOmniGateProjectionRow] = Field(default_factory=list)
    metrics: ModelOmniGateMetricsSnapshot = Field(
        default_factory=ModelOmniGateMetricsSnapshot
    )
    event: dict[str, Any] = Field(default_factory=dict)


__all__ = ["ModelOmniGateProjectionInput"]
