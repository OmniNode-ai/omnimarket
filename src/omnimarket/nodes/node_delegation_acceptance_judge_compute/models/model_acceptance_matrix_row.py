# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One model by task type by kind cell: true accept rate beside terminal_ok."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_delegation_acceptance_judge_compute.models.model_acceptance_failure_count import (
    ModelAcceptanceFailureCount,
)


class ModelAcceptanceMatrixRow(BaseModel):
    """One model by task type by kind cell: true accept rate beside terminal_ok."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str
    task_type: str
    kind: str
    judged: int = Field(ge=1)
    accepted: int = Field(ge=0)
    accept_rate: float = Field(ge=0.0, le=1.0)
    accept_low: float = Field(ge=0.0, le=1.0)
    accept_high: float = Field(ge=0.0, le=1.0)
    mean_quality: float = Field(ge=0.0, le=3.0)
    cell_events: int = Field(ge=0)
    terminal_ok_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    usable_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    thin: bool
    failures: tuple[ModelAcceptanceFailureCount, ...] = ()
