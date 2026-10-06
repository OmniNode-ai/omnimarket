# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The attachable draw record includes its source run directory."""

from typing import Literal

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_delegation_rubric_check_compute.models.enum_route_check_outcome import (
    EnumRouteCheckOutcome,
)
from omnimarket.nodes.node_delegation_rubric_check_compute.models.model_route_observation import (
    ModelRouteObservation,
)


class ModelRouteCheckVerdict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    outcome: EnumRouteCheckOutcome
    run_directory: str
    route: Literal["L", "C"]
    pin: str
    draw_class: Literal["REFUSED"] | None = None
    failure_reason: str | None = None
    observations: tuple[ModelRouteObservation, ...] = ()
    failures: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
