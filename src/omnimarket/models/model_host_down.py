# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed observations and readings for the host down node (OMN-19998)."""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EnumHostState(StrEnum):
    """The down reading of one lab host for one evaluation."""

    UP = "up"
    SUSPECT = "suspect"
    DOWN = "down"
    UNKNOWN = "unknown"


class ModelHostDownRules(BaseModel):
    """The down rule, carried in the request; the node owns no literal bound."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    down_bound_s: int = Field(ge=1)
    unknown_when_no_host_answers: bool


class ModelHostObservation(BaseModel):
    """One probe read of one host, plus what the node needs to bound it.

    ``probe_ok`` None means the probe could not run (not that the host failed);
    ``first_failed_at`` is the start of the current run of failed probes, from
    the prior reading; ``advertised_at`` is the newest capacity advertisement
    of the host, a host that advertised recently is alive even while its probe
    fails.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    host_name: str = Field(min_length=1)
    probe_ok: bool | None
    first_failed_at: datetime | None
    advertised_at: datetime | None


class ModelHostDownRequest(BaseModel):
    """One evaluation tick: when, the rules, and every host's observation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evaluated_at: datetime
    rules: ModelHostDownRules
    observations: tuple[ModelHostObservation, ...]

    @model_validator(mode="after")
    def _host_names_unique(self) -> "ModelHostDownRequest":
        names = [observation.host_name for observation in self.observations]
        if len(names) != len(set(names)):
            raise ValueError("host_name values must be unique")
        return self


class ModelHostReading(BaseModel):
    """The down reading of one host for one evaluation tick."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host_name: str
    state: EnumHostState
    first_failed_at: datetime | None
    down_since: datetime | None
    silent_seconds: float | None
    reason: str


class ModelHostDownReading(BaseModel):
    """Every host's reading for one evaluation tick."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evaluated_at: datetime
    readings: tuple[ModelHostReading, ...]

    @property
    def down_hosts(self) -> tuple[str, ...]:
        """The names of every host currently read DOWN, sorted."""
        return tuple(
            sorted(
                reading.host_name
                for reading in self.readings
                if reading.state == EnumHostState.DOWN
            )
        )
