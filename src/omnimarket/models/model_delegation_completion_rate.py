# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models of the delegation completion-rate reading (OMN-23108).

The definitions are the ones lane delegation-why-fl0510 gave over ``delegation_events``. A completion row is a real,
non-shadow row that ended ``terminal_ok`` and passed the quality gate. ``R_all`` counts them in the window. ``R_dep`` counts the
ones the deployed lane answered, which is every one whose answering backend does not start with the harness prefix; a row with no
answering backend is not a deployed answer (SQL ``NOT LIKE`` over NULL excludes it too). ``failed_rate`` is the share of the
window's real, non-shadow rows that did not end ``terminal_ok``. The window, the prefix, the data source word and every bound are
the overlay's (``ModelDelegationRateOverlay``); nothing here has a default.
"""

from __future__ import annotations

from datetime import UTC, timedelta
from typing import Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.model_reading import EnumReadingVerdict


def _utc(value: AwareDatetime, field: str) -> AwareDatetime:
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{field} is not UTC: {value.isoformat()}")
    return value.astimezone(UTC)


class ModelDelegationRateBounds(BaseModel):
    """The floor owner's bounds. The reading is WORKING only inside all three."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    min_r_all: int = Field(ge=1)
    min_r_dep: int = Field(ge=1)
    max_failed_rate: float = Field(ge=0.0, le=1.0)


class ModelDelegationRateOverlay(BaseModel):
    """The overlay of node_delegation_completion_rate_compute. No key has a default."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    window_seconds: int = Field(ge=60)
    real_data_source: str = Field(min_length=1)
    harness_backend_prefix: str = Field(min_length=1)
    publish_max_age_seconds: int = Field(ge=1)
    row_lane: str = Field(min_length=1)
    row_src: str = Field(min_length=1)
    bounds: ModelDelegationRateBounds


class ModelDelegationEventFact(BaseModel):
    """The columns of one ``delegation_events`` row the reading needs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_time: AwareDatetime
    data_source: str
    is_shadow: bool
    terminal_ok: bool | None
    quality_gate_passed: bool | None
    answering_backend: str | None

    @model_validator(mode="after")
    def _check(self) -> Self:
        _utc(self.event_time, "event_time")
        return self


class ModelDelegationCompletionRateRequest(BaseModel):
    """The rows the caller read, the overlay and the time to read at."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    now: AwareDatetime
    overlay: ModelDelegationRateOverlay
    events: tuple[ModelDelegationEventFact, ...]
    events_unread_reason: str | None

    @model_validator(mode="after")
    def _check(self) -> Self:
        _utc(self.now, "now")
        if self.events_unread_reason is not None:
            if not self.events_unread_reason.strip():
                raise ValueError("events_unread_reason names no reason")
            if self.events:
                raise ValueError("rows and an unread reason cannot both be given")
        return self


class ModelDelegationCompletionRate(BaseModel):
    """The reading at the read time, and the ledger row that carries it to the floor."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: EnumReadingVerdict
    reasons: tuple[str, ...]
    r_all: int | None
    r_dep: int | None
    failed_rate: float | None
    rows_in_window: int
    rows_future_dated: int
    window_seconds: int
    read_at: AwareDatetime
    ledger_row: str


__all__: list[str] = [
    "ModelDelegationCompletionRate",
    "ModelDelegationCompletionRateRequest",
    "ModelDelegationEventFact",
    "ModelDelegationRateBounds",
    "ModelDelegationRateOverlay",
]
