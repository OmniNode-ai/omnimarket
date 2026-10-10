# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Caller-supplied facts and pure product-floor alarm decisions (OMN-18008)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def parse_stamp(text: str) -> datetime:
    """Exactly queue_status.parse_stamp, including its minute-only UTC stamps."""
    fmt = "%Y-%m-%dT%H:%M:%SZ" if text.count(":") == 2 else "%Y-%m-%dT%H:%MZ"
    return datetime.strptime(text, fmt).replace(tzinfo=UTC)


class ModelProductFloorCloneSync(BaseModel):
    """Clone-sync facts already read by the caller; no clone paths are accessed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    last_run: str | None
    note: str | None
    clones: dict[str, list[str]]


class ModelProductFloorScan(BaseModel):
    """The caller's git scan result, including its positive control."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    landed: int
    count: int
    docs: int
    last: str | None


class ModelProductFloorVerdictRequest(BaseModel):
    """Every read and the clock belong to the caller.

    A repo with clones but neither a scan nor a scan error becomes
    ``floor:<repo>`` UNKNOWN with ``cannot count <repo>: no scan supplied``.
    The old controller silence threshold is the default of 30 minutes.
    Raw watcher objects and controller lines retain the old validation decisions.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    now: str
    window_hours: float = Field(default=2, gt=0, allow_inf_nan=False)
    max_input_age_minutes: float = Field(default=15, gt=0, allow_inf_nan=False)
    controller_ticks: int = Field(default=3, gt=0)
    controller_silent_after_minutes: float = 30
    floors: dict[str, float] | None = None
    floors_unknown: str | None = None
    watcher: dict[str, Any] | None = None
    watcher_unavailable: str | None = None
    clone_sync: ModelProductFloorCloneSync | None = None
    clone_sync_unavailable: str | None = None
    scans: dict[str, ModelProductFloorScan] = Field(default_factory=dict)
    scan_errors: dict[str, str] = Field(default_factory=dict)
    scan_notes: list[str] = Field(default_factory=list)
    controller_lines: list[str] | None = None
    controller_unavailable: str | None = None
    row_lane: str
    row_ticket: str

    @field_validator("now")
    @classmethod
    def validate_now(cls, value: str) -> str:
        parse_stamp(value)
        return value

    @model_validator(mode="after")
    def exactly_one_floor_source(self) -> Self:
        if (self.floors is None) == (self.floors_unknown is None):
            raise ValueError("exactly one of floors or floors_unknown must be supplied")
        return self


class ModelProductFloorRepoFacts(BaseModel):
    """Product merge rate and display detail for one positive-floor repository."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    floor: float
    count: int | None = None
    rate: float | None = None
    docs_only: int | None = None
    landed_seen: int | None = None
    last_product_merge: str | None = None
    detail: str


class ModelProductFloorControllerFacts(BaseModel):
    """Controller tail, including facts accumulated before a malformed row.

    Tick identifiers, statuses, refusals and degraded values are raw JSON cells:
    the old script does not validate their types. Worker totals alone are validated.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    last_ts: str | None = None
    last_age_minutes: float | None = None
    last_status: Any = None
    last_refusal: Any = None
    workers_per_tick: list[int] = Field(default_factory=list)
    ticks: list[Any] = Field(default_factory=list)
    statuses: list[Any] = Field(default_factory=list)
    degraded: list[Any] = Field(default_factory=list)
    detail: str = "controller UNKNOWN"


class ModelProductFloorVerdictResult(BaseModel):
    """Decision facts and exactly the old summary, breached status row and cell."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ts: str
    per_repo: dict[str, ModelProductFloorRepoFacts]
    waiting: dict[str, int] | None
    breaches: dict[str, str]
    unknowns: dict[str, str]
    controller: ModelProductFloorControllerFacts
    notes: list[str]
    verdict: Literal["BREACH", "UNKNOWN", "OK"]
    summary_lines: list[str]
    status_row: str
    controller_cell: str
