# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models of the daily delegation_events defect scan (OMN-23331).

Three defects are counted over the real, non-shadow rows of one day of ``delegation_events``:

``missing_terminal``
    the row is older than the terminal grace and its ``terminal_ok`` was never reduced (``NULL``): the attempt has no terminal
    outcome and will never get one.
``cost_unread_as_zero``
    the row spent tokens but records a cost of exactly zero with no ``cost_measurement_source``: an unread cost stored as a
    price (migration 0061 made ``cost_usd`` nullable so that an unpriced call could stop reading as free).
``unattributed``
    the row names no ``caller_lane``, so the attempt cannot be charged to anyone.

The day, the data source word, the grace and every bound are the overlay's (``ModelDefectScanOverlay``); nothing here has a
default. Facts carry no prompt, answer or identifier.
"""

from __future__ import annotations

from datetime import UTC, timedelta
from decimal import Decimal
from typing import Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.model_reading import EnumReadingVerdict


def _utc(value: AwareDatetime, field: str) -> AwareDatetime:
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{field} is not UTC: {value.isoformat()}")
    return value.astimezone(UTC)


class ModelDefectScanBounds(BaseModel):
    """The floor owner's bounds: the most rows of each defect a day may carry and still read WORKING."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    min_rows_scanned: int = Field(ge=1)
    max_missing_terminal: int = Field(ge=0)
    max_cost_unread_as_zero: int = Field(ge=0)
    max_unattributed: int = Field(ge=0)


class ModelDefectScanOverlay(BaseModel):
    """The overlay of node_delegation_events_defect_scan_compute. No key has a default."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    window_seconds: int = Field(ge=3600)
    terminal_grace_seconds: int = Field(ge=0)
    real_data_source: str = Field(min_length=1)
    publish_max_age_seconds: int = Field(ge=1)
    row_lane: str = Field(min_length=1)
    row_src: str = Field(min_length=1)
    bounds: ModelDefectScanBounds


class ModelDefectScanFact(BaseModel):
    """The columns of one ``delegation_events`` row the scan needs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_time: AwareDatetime
    data_source: str
    is_shadow: bool
    terminal_ok: bool | None
    cost_usd: Decimal | None
    cost_measurement_source: str | None
    tokens_total: int = Field(ge=0)
    caller_lane: str | None

    @model_validator(mode="after")
    def _check(self) -> Self:
        _utc(self.event_time, "event_time")
        return self


class ModelDefectScanRequest(BaseModel):
    """The rows the caller read, the overlay and the time to scan at."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    now: AwareDatetime
    overlay: ModelDefectScanOverlay
    events: tuple[ModelDefectScanFact, ...]
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


class ModelDefectScan(BaseModel):
    """The scan at its read time, and the ledger row that carries it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: EnumReadingVerdict
    reasons: tuple[str, ...]
    rows_scanned: int
    rows_future_dated: int
    missing_terminal: int | None
    cost_unread_as_zero: int | None
    unattributed: int | None
    window_seconds: int
    read_at: AwareDatetime
    ledger_row: str


__all__: list[str] = [
    "ModelDefectScan",
    "ModelDefectScanBounds",
    "ModelDefectScanFact",
    "ModelDefectScanOverlay",
    "ModelDefectScanRequest",
]
