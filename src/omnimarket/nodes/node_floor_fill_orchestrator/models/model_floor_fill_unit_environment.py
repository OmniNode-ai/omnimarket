# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The request and result of building the live tick unit's environment."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.overlay_keys.model_overlay_documents import ModelLabHostRow
from omnimarket.nodes.node_floor_fill_orchestrator.models.model_floor_fill_schedule import (
    ModelFloorFillSchedule,
)


class ModelFloorFillUnitEnvironmentRequest(BaseModel):
    """What the unit's environment is built from: the contract's schedule block, the fill host's row (or ``None``
    when the table holds none), the registry root, and the installing host's environment (already resolved)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schedule: ModelFloorFillSchedule
    host: str = Field(min_length=1)
    host_row: ModelLabHostRow | None
    omni_root: str = Field(min_length=1)
    installing_environment: dict[str, str]


class ModelFloorFillUnitEnvironmentResult(BaseModel):
    """The unit's variables, keyed by the names the contract declares."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    variables: dict[str, str]


__all__: list[str] = [
    "ModelFloorFillUnitEnvironmentRequest",
    "ModelFloorFillUnitEnvironmentResult",
]
