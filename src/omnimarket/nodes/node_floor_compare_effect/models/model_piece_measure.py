# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed models of each live rebuild piece's measure against the old way (OMN-23081).

The overlay declares, per live piece, the outcome measure (a key of the floor's compare history), the date the piece went
live and how many days before it make the baseline. The comparison reads the baseline from the history before the live date,
each of the first 7 days after it, and on day 7 a verdict of better, worse or unread. A verdict is never inferred: a measure
with too few points before or after the live date is unread, and a recorded verdict is kept as recorded.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_floor_compare_effect.models.model_floor_compare import (
    Polarity,
)


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


MeasureVerdict = Literal["better", "worse", "same", "unread"]
PieceVerdict = Literal["better", "worse", "unread", "pending"]


class ModelMeasureDecl(_Frozen):
    """One outcome measure: a compare-history key, its words and which way is better."""

    key: str = Field(min_length=1, description="A key of the compare history.")
    label: str = Field(min_length=1)
    unit: str
    polarity: Polarity
    source: str = Field(
        min_length=1, description="Where the dated points of this key come from."
    )
    min_change: float = Field(
        default=0.0,
        ge=0.0,
        description="A relative change at most this big reads same (0.1 is 10 percent of the baseline, at least 1 unit).",
    )


class ModelPieceMeasureDecl(_Frozen):
    piece: int = Field(ge=1, le=18)
    name: str = Field(min_length=1)
    call: str = Field(
        min_length=1, description="The plan's keep, wire, port or rewrite call."
    )
    live_at: datetime
    live_source: str = Field(
        min_length=1, description="The read the live date comes from."
    )
    measures: tuple[ModelMeasureDecl, ...] = Field(min_length=1)


class ModelPieceMeasuresOverlay(_Frozen):
    baseline_days: int = Field(
        ge=1, description="Days before the live date that make the baseline."
    )
    trial_days: int = Field(
        ge=1, description="Days after the live date before the verdict."
    )
    min_points: int = Field(
        ge=1, description="Fewest points a baseline, or the trial, needs to be read."
    )
    verdicts_relpath: str = Field(
        min_length=1, description="Append-only record of the day-7 verdicts."
    )
    pieces: tuple[ModelPieceMeasureDecl, ...]


class ModelMeasureDay(_Frozen):
    day: int
    start: datetime
    end: datetime
    value: float | None
    points: int


class ModelMeasureResult(_Frozen):
    key: str
    label: str
    unit: str
    polarity: Polarity
    source: str
    baseline: float | None
    baseline_points: int
    baseline_from: datetime
    baseline_to: datetime
    days: tuple[ModelMeasureDay, ...]
    trial: float | None
    trial_points: int
    verdict: MeasureVerdict | None
    words: str


class ModelPieceMeasureResult(_Frozen):
    piece: int
    name: str
    call: str
    live_at: datetime
    live_source: str
    day: int = Field(
        description="Day of the trial now (1 on the live date's first 24 hours)."
    )
    trial_days: int
    verdict_due: datetime
    verdict: PieceVerdict
    verdict_at: datetime | None = None
    recorded: bool = False
    reason: str
    measures: tuple[ModelMeasureResult, ...]


class ModelPieceVerdictRecord(_Frozen):
    """One line of the verdict record: written once on day 7 and never rewritten."""

    piece: int
    live_at: datetime
    verdict: Literal["better", "worse", "unread"]
    verdict_at: datetime
    reason: str
    measures: dict[str, str]
