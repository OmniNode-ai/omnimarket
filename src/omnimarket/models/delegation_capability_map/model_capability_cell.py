# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One capability cell: a model, backend and configuration on a class at a band."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.delegation.model_size_band import EnumSizeBand
from omnimarket.models.delegation.wire.model_attempt_configuration import (
    ModelAttemptConfiguration,
)
from omnimarket.models.delegation_capability_map.enum_capability_cell_verdict import (
    EnumCapabilityCellVerdict,
)


class ModelCapabilityCell(BaseModel):
    """n, passes, the Wilson 95 percent interval and the range evaluator's decision bound.

    A cell exists only where rows exist: a band with no rows has no cell, never a zero one.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: UUID
    map_run_id: str = Field(min_length=1)
    model_key: str = Field(min_length=1)
    backend_id: str = Field(min_length=1)
    configuration_id: str = Field(min_length=1)
    configuration_parameters: ModelAttemptConfiguration
    task_class: str = Field(min_length=1)
    size_band: EnumSizeBand
    n: int = Field(ge=1)
    passes: int = Field(ge=0)
    pass_rate: float = Field(ge=0.0, le=1.0)
    wilson_low: float = Field(ge=0.0, le=1.0)
    wilson_high: float = Field(ge=0.0, le=1.0)
    decision_lower_bound: float = Field(ge=0.0, le=1.0)
    floor: float = Field(gt=0.0, lt=1.0)
    required_n: int = Field(ge=1)
    verdict: EnumCapabilityCellVerdict
