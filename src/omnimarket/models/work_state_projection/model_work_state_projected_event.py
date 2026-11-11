# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The scheduled work-state projection as an event on the bus, and the schedule's overlay (OMN-22935)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnimarket.nodes.node_reading_writer_effect.overlay import (
    ModelCommandLedgerReadRoute,
)


class EnumWorkStateProjectorProfile(StrEnum):
    """Which projector the schedule binds: the in-memory default or the file sources the lab runtime reads."""

    UNREAD = "unread"
    FILE_SOURCES = "file_sources"


class ModelWorkStateScheduleOverlay(BaseModel):
    """The schedule's overlay: the cadence of the scheduled projection and the projector profile it binds."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_interval_seconds: int = Field(gt=0)
    projector: EnumWorkStateProjectorProfile = Field(
        description="unread (in-memory default: every source drawn unread) or file_sources (the real-backend profile).",
    )
    state_home: str | None = Field(
        default=None,
        description=(
            "An absolute directory; when set, the file_sources profile reads and writes every "
            "home-relative path (projection file, source snapshots, feeder inputs) under it instead "
            "of under OMNI_HOME. The runtime sets a directory on its own data volume."
        ),
    )
    ledger_read: ModelCommandLedgerReadRoute | None = Field(
        default=None,
        description=(
            "When set, the file_sources profile reads ledger rows through the bus read command "
            "that route names (command_env, timeout_seconds, row_limit) and never reads a ledger "
            "file; when None the profile keeps its file route."
        ),
    )

    @field_validator("state_home")
    @classmethod
    def _state_home_is_absolute(cls, value: str | None) -> str | None:
        if value is not None and not PurePosixPath(value).is_absolute():
            raise ValueError("state_home must be an absolute path")
        return value


class ModelWorkStateProjectedEvent(BaseModel):
    """What one scheduled projection run reports on the bus: the counts, the ready count and every source not fresh.

    The items themselves are not carried: the projection is written where the floor and the fill dispatcher read it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    computed_at: datetime = Field(
        description="The tick time the projection was computed for."
    )
    tick_id: str = Field(description="The runtime tick that scheduled the run.")
    projector: EnumWorkStateProjectorProfile
    as_of: datetime = Field(description="The projection's own clock reading.")
    item_count: int = Field(ge=0)
    counts: dict[str, int]
    ready_in_scope: int = Field(ge=0)
    unread: tuple[str, ...] = ()
    stale_sources: tuple[str, ...] = ()


__all__ = [
    "EnumWorkStateProjectorProfile",
    "ModelWorkStateProjectedEvent",
    "ModelWorkStateScheduleOverlay",
]
