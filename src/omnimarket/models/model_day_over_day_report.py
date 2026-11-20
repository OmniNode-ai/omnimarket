# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request and report of the day-over-day report (OMN-26212).

One shared module so the factory floor panel and any other reader take the report from the same model. Every figure carries
its source and a read time, and reads ``UNREADABLE`` (never 0, never blank) when its source is missing.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

UNREADABLE = "UNREADABLE"
Reading = Literal["read", "UNREADABLE"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelRunningReading(_Frozen):
    """One change as the integration pass or a STATUS row (installed=, observed=) read it on a day."""

    change: str = Field(min_length=1)
    host: str = Field(min_length=1)
    state: Literal["running", "not_running"]
    source: str = Field(min_length=1)
    read_at: datetime


class ModelBoardReading(_Frozen):
    """One beta board check (C11 to C28) on a day."""

    check_id: str = Field(min_length=1)
    verdict: Literal["green", "red"]
    source: str = Field(min_length=1)
    read_at: datetime


class ModelCostReading(_Frozen):
    """Calls and tokens by engine on a day; dollars only where a recorded source exists (else None)."""

    engine: str = Field(min_length=1)
    calls: int | None = Field(default=None, ge=0)
    tokens: int | None = Field(default=None, ge=0)
    dollars: float | None = Field(default=None, ge=0)
    source: str = Field(min_length=1)
    read_at: datetime


class ModelDayReadings(_Frozen):
    """What was read for one day window. A source that was not read is None, never an empty tuple."""

    label: str = Field(min_length=1)
    running: tuple[ModelRunningReading, ...] | None = None
    board: tuple[ModelBoardReading, ...] | None = None
    cost: tuple[ModelCostReading, ...] | None = None
    merges: int | None = Field(default=None, ge=0)
    merges_source: str = ""


class ModelDayOverDayRequest(_Frozen):
    """The two day windows; the caller supplies the time."""

    yesterday: ModelDayReadings
    today: ModelDayReadings
    as_of: datetime


class ModelFigure(_Frozen):
    """One number with its source and read time; ``value`` is ``UNREADABLE`` when the source is missing."""

    value: int | float | Literal["UNREADABLE"]
    source: str = Field(min_length=1)
    read_at: datetime | None = None


class ModelNewlyRunning(_Frozen):
    change: str
    host: str
    source: str
    read_at: datetime


class ModelBoardChange(_Frozen):
    check_id: str
    before: Literal["green", "red"]
    after: Literal["green", "red"]
    source: str
    read_at: datetime


class ModelRegression(_Frozen):
    kind: Literal["stopped_running", "board_red"]
    subject: str
    detail: str
    source: str
    read_at: datetime


class ModelCostEntry(_Frozen):
    engine: str
    calls_yesterday: ModelFigure
    calls_today: ModelFigure
    tokens_yesterday: ModelFigure
    tokens_today: ModelFigure
    dollars_yesterday: ModelFigure
    dollars_today: ModelFigure


class ModelNewlyRunningSection(_Frozen):
    reading: Reading
    entries: tuple[ModelNewlyRunning, ...] = ()


class ModelBoardChangeSection(_Frozen):
    reading: Reading
    entries: tuple[ModelBoardChange, ...] = ()


class ModelRegressionSection(_Frozen):
    reading: Reading
    entries: tuple[ModelRegression, ...] = ()


class ModelCostSection(_Frozen):
    reading: Reading
    entries: tuple[ModelCostEntry, ...] = ()


class ModelThroughput(_Frozen):
    merges_yesterday: ModelFigure
    merges_today: ModelFigure
    running_yesterday: ModelFigure
    running_today: ModelFigure


class ModelDayRow(_Frozen):
    """One row per day for a trend panel: a null is a figure whose source was not read (the panel shows UNREADABLE)."""

    date: str
    merged: int | None
    seen_running: int | None
    cost_usd: float | None
    tokens: int | None


class ModelDayOverDayReport(_Frozen):
    """The report a dashboard panel reads."""

    yesterday_label: str
    today_label: str
    as_of: datetime
    newly_running: ModelNewlyRunningSection
    board_changes: ModelBoardChangeSection
    throughput: ModelThroughput
    cost_trend: ModelCostSection
    regressions: ModelRegressionSection
    days: tuple[ModelDayRow, ...]
