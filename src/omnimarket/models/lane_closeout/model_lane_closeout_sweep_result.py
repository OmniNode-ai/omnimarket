# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Result of one host-local lane closeout sweep."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.lane_closeout.model_lane_closeout_record import (
    ModelLaneCloseoutRecord,
)


class ModelLaneCloseoutSweepResult(BaseModel):
    """What the sweep read and closed. ``unrecorded`` is the reading: ended lanes with no record."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    host: str
    ended_lanes: int = Field(description="Lane runs past their grace, recorded or not.")
    unrecorded: int = Field(
        description="Ended lane runs with no closeout record after this sweep."
    )
    unrecorded_lanes: tuple[str, ...] = Field(default=())
    closed: tuple[ModelLaneCloseoutRecord, ...] = Field(default=())
    skipped_running: tuple[str, ...] = Field(default=())
    skipped_live: tuple[str, ...] = Field(default=())
    read_only: bool = False
