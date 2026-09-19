# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One materialized runner-fleet read-model row (OMN-18768)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_projection_runner_fleet.models.enum_runner_status import (
    EnumRunnerStatus,
)


class ModelRunnerFleetRow(BaseModel):
    """A row of ``runner_fleet_liveness``, keyed on ``runner_name``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    runner_name: str = Field(min_length=1)
    runner_id: int | None = None
    label_class: str = Field(min_length=1)
    labels: tuple[str, ...] = ()
    host: str = Field(min_length=1)
    observing_host: str = Field(min_length=1)
    status: EnumRunnerStatus
    current_job_id: str | None = None
    observed_at: datetime
