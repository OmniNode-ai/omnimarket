# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 OmniNode Team
"""A named escalation tier with one or more model candidates."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_delegation_routing_reducer.models.model_tier_model import (
    ModelTierModel,
)


class ModelRoutingTier(BaseModel):
    """A named escalation tier with one or more model candidates."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(..., description="Tier name: local, cheap_cloud, or claude.")
    models: tuple[ModelTierModel, ...] = Field(
        default_factory=tuple,
        description="Ordered list of candidate models in this tier.",
    )
    eval_before_accept: bool = Field(
        default=False,
        description="Whether to run eval before accepting result from this tier.",
    )
    eval_model: str | None = Field(
        default=None,
        description="Model to use for eval scoring (tier name required).",
    )
    max_retries: int = Field(
        default=0,
        description="Max retry attempts within this tier before escalating.",
    )


__all__: list[str] = ["ModelRoutingTier"]
