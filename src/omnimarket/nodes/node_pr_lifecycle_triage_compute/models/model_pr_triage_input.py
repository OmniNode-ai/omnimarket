# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelPrTriageInput — input to the PR lifecycle triage compute node.

Related:
    - OMN-8083: pr_lifecycle_triage_compute
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.model_pr_inventory_item import (
    ModelPrInventoryItem,
)


class ModelPrTriageInput(BaseModel):
    """Input to the PR triage compute node — inventory data from pr_lifecycle_inventory_compute."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(
        ..., description="Correlation ID from the inventory event."
    )
    prs: tuple[ModelPrInventoryItem, ...] = Field(
        ..., description="PR inventory items to classify."
    )


__all__: list[str] = ["ModelPrTriageInput"]
