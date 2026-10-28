# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and result of the delegation seam orchestrator's read plan."""

from __future__ import annotations

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class ModelSeamReadPlanRequest(BaseModel):
    """The read time and, optionally, the workflow ids the readings are for."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    now: AwareDatetime
    workflow_ids: tuple[str, ...] = ()


class ModelSeamReadStep(BaseModel):
    """One reader operation the orchestrator calls, on one node."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    node: str = Field(min_length=1)
    operation: str = Field(min_length=1)


class ModelSeamReadPlan(BaseModel):
    """The ordered reader operations of the delegation seam readings."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    planned_at: AwareDatetime
    workflow_ids: tuple[str, ...]
    steps: tuple[ModelSeamReadStep, ...]
