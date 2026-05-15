# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Completed event model for node_dependency_health_sweep."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_dependency_health_sweep.models.model_dep_health_finding import (
    ModelDepHealthFinding,
)


class ModelDepHealthSweepCompletedEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    findings: list[ModelDepHealthFinding]
    summary: dict[str, int]
    captured_at: datetime
