# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The cost-and-throughput panel of the factory floor (schema id omnimarket.floor_cost_throughput.v1) and its request.

Every figure carries its source (the node that made the reading, the data read, the read time) and is either a number or the
word UNREADABLE with the reason. A figure whose source is missing is never 0 and never blank. A dollar cell with no recorded dollar
source is UNREADABLE; the call or token count beside it is still shown.
"""

from __future__ import annotations

from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_ID: Final = "omnimarket.floor_cost_throughput.v1"
UNREADABLE: Final = "UNREADABLE"

Engine = Literal["Claude", "Codex", "GLM", "local"]
ENGINES: Final[tuple[Engine, ...]] = ("Claude", "Codex", "GLM", "local")
TREND_DAYS: Final = 7


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# ---- the request: the readings other nodes made -----------------------------------------------------------------------------


class ModelSourceDocument(_Frozen):
    """One document another node wrote, with who wrote it, what was read from it and when it was read."""

    node: str = Field(min_length=1)
    data_read: str = Field(min_length=1)
    read_at: str = Field(min_length=1, description="UTC stamp, YYYY-MM-DDTHH:MM:SSZ")
    document: dict[str, Any]


class ModelSeenRunningPoint(_Frozen):
    """One reading of the integration record's cumulative counts."""

    read_at: str = Field(min_length=1)
    merged: int = Field(ge=0)
    seen_running: int = Field(ge=0)


class ModelSeenRunningSeries(_Frozen):
    """The kept readings of the integration record, oldest first, and the node that made them."""

    node: str = Field(min_length=1)
    data_read: str = Field(min_length=1)
    points: tuple[ModelSeenRunningPoint, ...]


class ModelDayOverDayDay(_Frozen):
    """One UTC date of the day-over-day report. A figure the report did not read is None."""

    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    merged: int | None = None
    seen_running: int | None = None
    cost_usd: float | None = None
    tokens: int | None = None


class ModelDayOverDayReport(_Frozen):
    """The output of the day-over-day report node, as the panel reads it for the trend."""

    node: str = Field(min_length=1)
    generated_at: str = Field(min_length=1)
    days: tuple[ModelDayOverDayDay, ...]


class ModelCostThroughputRequest(_Frozen):
    """The readings. A reading that is None was not made: its figures read UNREADABLE."""

    generated_at: str = Field(
        min_length=1, description="UTC stamp of this fold, from the caller's clock"
    )
    floor_simple: ModelSourceDocument | None = None
    delegation_history: ModelSourceDocument | None = None
    seen_running_series: ModelSeenRunningSeries | None = None
    day_over_day: ModelDayOverDayReport | None = None


# ---- the served panel --------------------------------------------------------------------------------------------------------


class ModelFigureSource(_Frozen):
    node: str = Field(min_length=1)
    data_read: str = Field(min_length=1)
    read_at: str | None = None


class ModelFigure(_Frozen):
    """One number with its source, or UNREADABLE with the reason."""

    key: str = Field(min_length=1)
    label: str = Field(min_length=1)
    unit: str
    value: float | None
    display: str = Field(min_length=1)
    source: ModelFigureSource
    reason: str = ""

    @model_validator(mode="after")
    def _unreadable_names_its_reason(self) -> ModelFigure:
        if self.value is None:
            if self.display != UNREADABLE or not self.reason:
                raise ValueError(
                    "a figure with no value reads UNREADABLE and names why"
                )
        elif self.display == UNREADABLE:
            raise ValueError("a figure with a value does not read UNREADABLE")
        return self


class ModelEngineRow(_Frozen):
    engine: Engine
    calls: ModelFigure
    tokens: ModelFigure
    dollars: ModelFigure


class ModelTrendDay(_Frozen):
    date: str
    merged: ModelFigure
    seen_running: ModelFigure
    cost_usd: ModelFigure


class ModelTrend(_Frozen):
    status: Literal["read", "UNREADABLE"]
    reason: str = ""
    source: ModelFigureSource
    days: tuple[ModelTrendDay, ...]


class ModelCostThroughputPanel(_Frozen):
    """The served document cost-throughput.json."""

    schema_id: Literal["omnimarket.floor_cost_throughput.v1"] = SCHEMA_ID
    generated_at: str
    merges_per_hour: ModelFigure
    seen_running_per_hour: ModelFigure
    engines: tuple[ModelEngineRow, ...]
    cost_per_change_seen_running: ModelFigure
    trend: ModelTrend
    unreadable_count: int = Field(ge=0)
    unreadable_reasons: tuple[str, ...]
