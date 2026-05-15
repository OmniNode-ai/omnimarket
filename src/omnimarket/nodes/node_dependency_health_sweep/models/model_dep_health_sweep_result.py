# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Result model for node_dependency_health_sweep."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_dependency_health_sweep.models.model_dep_health_finding import (
    ModelDepHealthFinding,
)


class ModelDepHealthSweepResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: str
    run_id: str
    findings: list[ModelDepHealthFinding]
    summary: dict[str, int]
    baseline_delta: int | None = None
    graphify_version: str
