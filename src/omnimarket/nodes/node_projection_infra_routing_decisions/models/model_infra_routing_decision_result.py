# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The fold's output: the one row a delivery becomes."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_projection_infra_routing_decisions.models.model_infra_routing_decision_row import (
    ModelInfraRoutingDecisionRow,
)


class ModelInfraRoutingDecisionProjectionResult(BaseModel):
    """The row to persist."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    row: ModelInfraRoutingDecisionRow = Field(description="The row to upsert.")


__all__: list[str] = ["ModelInfraRoutingDecisionProjectionResult"]
