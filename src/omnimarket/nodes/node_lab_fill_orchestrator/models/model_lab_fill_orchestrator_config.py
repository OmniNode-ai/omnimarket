# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ``config.lab_fill_orchestrator`` block of the node contract (OMN-20867)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelLabFillOrchestratorConfig(BaseModel):
    """Which fires the orchestrator plans and how much it remembers."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    workflow: str = Field(..., min_length=1)
    max_hosts: int = Field(..., ge=1)
    max_fires_remembered: int = Field(..., ge=1)
    max_lanes: int = Field(..., ge=1, le=12)


__all__: list[str] = ["ModelLabFillOrchestratorConfig"]
