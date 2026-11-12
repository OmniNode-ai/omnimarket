# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed selected-service requirement observation request."""

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_onex_setup_effect.models.model_setup_plan import (
    ModelSetupPlan,
)


class ModelSetupStackRequirementsRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    plan: ModelSetupPlan
    services: tuple[str, ...] = Field(min_length=1)
