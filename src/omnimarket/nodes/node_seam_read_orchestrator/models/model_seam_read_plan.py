# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and plan of node_seam_read_orchestrator (OMN-21459)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from pydantic import BaseModel, ConfigDict


class ModelSeamReadPlanRequest(BaseModel):
    """The execution graph nodes of the factory line contract, in contract order."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    graph_nodes: Sequence[Mapping[str, object]]


class ModelSeamReadStep(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    node_id: str
    operation: str


class ModelSeamReadPlan(BaseModel):
    """The seam readings to run, in line order."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    steps: tuple[ModelSeamReadStep, ...]
