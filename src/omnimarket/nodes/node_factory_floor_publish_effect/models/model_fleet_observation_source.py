# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the existing runner projection's floor file from its runtime host."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_factory_floor_publish_effect.models.model_delegation_report import (
    ModelDelegationReadOverlay,
)


class ModelFleetObservationSourceRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    overlay: ModelDelegationReadOverlay | None = None
    timeout_seconds: int = Field(default=30, gt=0)


class ModelFleetObservationSourceResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    document: dict[str, Any]
