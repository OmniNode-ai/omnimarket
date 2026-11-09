# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed models of piece 7's reading (OMN-22943, rebuild plan 3.5 row 7)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.model_reading import EnumReadingVerdict


class EnumDrillSend(StrEnum):
    """What the test admission instance answered to one send."""

    REFUSED = "refused"
    ADMITTED = "admitted"
    ERRORED = "errored"


class ModelDrillObservation(BaseModel):
    """What one drill saw on a test admission instance: a stop, a send above it, a control send elsewhere."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    drill_id: str = Field(min_length=1)
    probed_at: datetime
    #: The drill's lane is the piece the stop was opened on; the control lane is another piece.
    drill_lane: str = Field(min_length=1)
    control_lane: str = Field(min_length=1)
    stop_committed: bool
    drill_send: EnumDrillSend
    control_send: EnumDrillSend


class ModelReplayObservation(BaseModel):
    """What the replay over the recorded dispatch history saw."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    probed_at: datetime
    dispatches: int = Field(ge=0)
    mismatches: int = Field(ge=0)


class ModelProcessSpineReadingRequest(BaseModel):
    """The drill, the replay and the bounds the overlay gives the reading."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    drill: ModelDrillObservation | None = None
    replay: ModelReplayObservation | None = None
    replay_required: bool
    now: datetime
    max_age_seconds: int = Field(gt=0)


class ModelProcessSpineReading(BaseModel):
    """Piece 7's reading at a time, with the reason codes that decided it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: EnumReadingVerdict
    reasons: tuple[str, ...]
    probed_at: datetime | None
    published_at: datetime
    max_age_seconds: int
    drill_id: str | None = None
