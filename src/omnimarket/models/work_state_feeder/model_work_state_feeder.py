# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The scheduled work-state feeders as events on the bus, and the schedule's overlay (OMN-23427)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EnumFeeder(StrEnum):
    """Which feeder the schedule binds: the linear lanes, the readings, or the receipts."""

    LINEAR = "linear"
    READINGS = "readings"
    RECEIPTS = "receipts"


class EnumFeederProfile(StrEnum):
    """Which feeder the schedule runs: the in-memory default or the sources the lab runtime reads."""

    IN_MEMORY = "in_memory"
    LIVE = "live"


class ModelFeederScheduleOverlay(BaseModel):
    """The schedule's overlay: the cadence of the scheduled feeder, the stale bound and the profile it binds."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_interval_seconds: int = Field(gt=0)
    max_source_age_s: int = Field(
        gt=0,
        description="a fed source whose newest fact is older than this is reported stale.",
    )
    profile: EnumFeederProfile


class ModelFeederOutcome(BaseModel):
    """What one feeder run reports through its port: the newest fact written and what was not fed.

    ``source_as_of`` is the time of the newest fact written, not the time of the write.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["fed", "not_fed", "failed"]
    source_as_of: datetime | None = None
    item_count: int = Field(ge=0)
    cause: str = ""

    @model_validator(mode="after")
    def _status_names_its_evidence(self) -> ModelFeederOutcome:
        if self.status == "fed" and self.source_as_of is None:
            raise ValueError(
                "a fed outcome needs the source_as_of: the time of the newest fact written."
            )
        if self.status != "fed" and not self.cause:
            raise ValueError(
                "an outcome that was not fed needs a cause naming what was not fed."
            )
        return self


class ModelWorkStateFeederEvent(BaseModel):
    """What one scheduled feeder run reports on the bus: the status, the newest fact and the cause.

    The facts themselves are not carried: the feeder writes them where the floor reads them.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    feeder: EnumFeeder
    computed_at: datetime = Field(
        description="The tick time the feeder run was computed for."
    )
    tick_id: str = Field(description="The runtime tick that scheduled the run.")
    profile: EnumFeederProfile
    status: Literal["fed", "stale", "not_fed", "failed"]
    source_as_of: datetime | None = None
    item_count: int = Field(ge=0)
    cause: str = ""


__all__ = [
    "EnumFeeder",
    "EnumFeederProfile",
    "ModelFeederOutcome",
    "ModelFeederScheduleOverlay",
    "ModelWorkStateFeederEvent",
]
