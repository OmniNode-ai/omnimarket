# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay the nightly sender reads: every setting of the node, one optional (the mounted config root)."""

from __future__ import annotations

from datetime import time
from typing import Annotated

from pydantic import ConfigDict, Field, field_validator, model_validator

from omnimarket.models.schedule_trigger.model_schedule_trigger import (
    ModelScheduleDeclaration,
)

ConfigRoot = Annotated[str, Field(pattern=r"^/[A-Za-z0-9._/-]*$")]


def _time_of_day(value: object) -> object:
    """Read ``HH:MM`` (quoted in the overlay, since an unquoted 02:30 is a number in YAML)."""
    if isinstance(value, str):
        hour, separator, minute = value.partition(":")
        if separator and hour.isdigit() and minute.isdigit():
            return time(int(hour), int(minute))
    return value


class ModelNightlySenderScheduleOverlay(ModelScheduleDeclaration):
    """Which nightly to follow, when to look for it, and where the sender keeps its record of what it sent."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str = Field(
        pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$",
        description="The repository whose nightly workflow is followed.",
    )
    workflow_file: str = Field(
        pattern=r"^[A-Za-z0-9_.-]+$",
        description="The nightly workflow's file name.",
    )
    nightly_slot_time_utc: time = Field(
        description="The nightly's scheduled UTC time of day; a slot date and its lag are counted from it."
    )
    earliest_check_time_utc: time = Field(
        description="The UTC time of day after which a tick looks for the night's run."
    )
    settle_margin_seconds: int = Field(
        ge=0,
        description="Seconds a finished run is left alone before the commands go out, so its artifacts have settled.",
    )
    collector_max_lag_hours: int = Field(
        ge=1,
        description="Hours after the slot by which the night must have been sent; later it is a missed night.",
    )
    state_dir_env: str = Field(
        pattern=r"^[A-Z][A-Z0-9_]*$",
        description="The environment variable naming the writable directory that holds the sent record.",
    )
    bus_lane: str = Field(
        min_length=1,
        description="The declared bus lane the commands are published on.",
    )
    command_timeout_seconds: int = Field(
        ge=1,
        description="Seconds to wait for the terminal event that answers one command.",
    )
    workspace_config_root: ConfigRoot | None = Field(
        default=None,
        description=(
            "The directory holding config/lab/ci_bus_lanes.yaml in the runtime that runs the node. A runtime that mounts "
            "the declaration but exports no ONEX_WORKSPACE_CONFIG_ROOT cannot resolve the bus lane; a root the "
            "environment already names wins."
        ),
    )

    @field_validator("nightly_slot_time_utc", "earliest_check_time_utc", mode="before")
    @classmethod
    def _read_time(cls, value: object) -> object:
        return _time_of_day(value)

    @model_validator(mode="after")
    def _check_after_slot(self) -> ModelNightlySenderScheduleOverlay:
        if self.earliest_check_time_utc <= self.nightly_slot_time_utc:
            raise ValueError(
                "earliest_check_time_utc must be later in the day than nightly_slot_time_utc"
            )
        return self


__all__: list[str] = ["ModelNightlySenderScheduleOverlay"]
