# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and result of the capability map fold (OMN-20169)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.delegation_capability_map.model_capability_best_configuration import (
    ModelCapabilityBestConfiguration,
)
from omnimarket.models.delegation_capability_map.model_capability_cell import (
    ModelCapabilityCell,
)
from omnimarket.models.delegation_capability_map.model_capability_inadmissible_count import (
    ModelCapabilityInadmissibleCount,
)
from omnimarket.models.delegation_capability_map.model_capability_line import (
    ModelCapabilityLine,
)
from omnimarket.models.delegation_capability_map.model_capability_serving_assignment import (
    ModelCapabilityServingAssignment,
)
from omnimarket.models.delegation_capability_map.model_capability_verdict_row import (
    ModelCapabilityVerdictRow,
)


class ModelCapabilityMapFoldRequest(BaseModel):
    """One map run: its verdict rows and the serving assignments they are admitted under.

    The fold is a function of this request alone. ``map_run_id`` is part of every
    cell key, so two map runs never pool.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    map_run_id: str = Field(min_length=1)
    rows: tuple[ModelCapabilityVerdictRow, ...] = ()
    assignments: tuple[ModelCapabilityServingAssignment, ...] = ()
    line: ModelCapabilityLine = Field(default_factory=ModelCapabilityLine)


class ModelCapabilityMapFoldResult(BaseModel):
    """The cells, the best configurations read from them, and every refusal counted."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    map_run_id: str = Field(min_length=1)
    cells: tuple[ModelCapabilityCell, ...] = ()
    best_configurations: tuple[ModelCapabilityBestConfiguration, ...] = ()
    folded_row_count: int = Field(default=0, ge=0)
    inadmissible: tuple[ModelCapabilityInadmissibleCount, ...] = ()
