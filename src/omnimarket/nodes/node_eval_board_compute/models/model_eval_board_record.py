# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The typed record of the Model tests tab (OMN-24174, plan task T8).

Every percentage in the record sits in a ``percent`` field beside its count, and is ``None``
when the count is under the overlay's minimum. No field carries an id: a model is its plain
label, a test is its date.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class EnumEvalBoardMarkerKind(StrEnum):
    MODEL_VERSION = "model_version"
    PROMPT_OR_SETTINGS = "prompt_or_settings"
    SCORER = "scorer"
    TASK_SET = "task_set"


class EnumEvalBoardVerdict(StrEnum):
    """The range line's verdict for a size cell, the capability map's own three values."""

    MET = "MET"
    MISSED = "MISSED"
    REFUSED = "REFUSED"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelEvalBoardRate(_Frozen):
    """A count and, when the count is enough to read, its percentage and likely range."""

    n: int
    passed: int
    percent: int | None
    likely_low: int | None
    likely_high: int | None
    too_few: bool
    text: str


class ModelEvalBoardPoint(_Frozen):
    """One point of Chart 1: one run-rate row. ``hollow`` is a point from too few tasks."""

    tested_at: datetime
    rate: ModelEvalBoardRate
    hollow: bool


class ModelEvalBoardMarker(_Frozen):
    """A change between two consecutive points, or the start of a new set of test tasks."""

    kind: EnumEvalBoardMarkerKind
    point_index: int
    at: datetime
    label: str


class ModelEvalBoardSeries(_Frozen):
    model_label: str
    points: tuple[ModelEvalBoardPoint, ...]
    markers: tuple[ModelEvalBoardMarker, ...]
    note: str


class ModelEvalBoardChart1(_Frozen):
    title: str
    above: str
    below: str
    series: tuple[ModelEvalBoardSeries, ...]


class ModelEvalBoardSizeCell(_Frozen):
    size: str
    rate: ModelEvalBoardRate | None
    verdict: EnumEvalBoardVerdict | None
    reliable: bool


class ModelEvalBoardSizeRow(_Frozen):
    model_label: str
    tested_at: datetime
    cells: tuple[ModelEvalBoardSizeCell, ...]
    reliable_up_to: str | None
    reliable_text: str
    short_band_notes: tuple[str, ...]


class ModelEvalBoardChart3(_Frozen):
    title: str
    above: str
    below: str
    rows: tuple[ModelEvalBoardSizeRow, ...]


class ModelEvalBoardLiveRow(_Frozen):
    model_label: str
    week: str
    rate: ModelEvalBoardRate


class ModelEvalBoardLiveWork(_Frozen):
    title: str
    above: str
    rows: tuple[ModelEvalBoardLiveRow, ...]


class ModelEvalBoardDefinition(_Frozen):
    term: str
    definition: str


class ModelEvalBoardTested(_Frozen):
    model_label: str
    tested_at: datetime | None
    text: str


class ModelEvalBoardFooter(_Frozen):
    published_at: datetime
    published_text: str
    last_tested: tuple[ModelEvalBoardTested, ...]
    void_notes: tuple[str, ...]
    checks_text: str


class ModelEvalBoardRecord(_Frozen):
    """The Model tests tab's content: headline, Chart 1, Chart 3, Live work, glossary, footer."""

    headline: str
    chart1: ModelEvalBoardChart1
    chart3: ModelEvalBoardChart3
    live_work: ModelEvalBoardLiveWork
    glossary: tuple[ModelEvalBoardDefinition, ...]
    footer: ModelEvalBoardFooter


__all__ = [
    "EnumEvalBoardMarkerKind",
    "EnumEvalBoardVerdict",
    "ModelEvalBoardChart1",
    "ModelEvalBoardChart3",
    "ModelEvalBoardDefinition",
    "ModelEvalBoardFooter",
    "ModelEvalBoardLiveRow",
    "ModelEvalBoardLiveWork",
    "ModelEvalBoardMarker",
    "ModelEvalBoardPoint",
    "ModelEvalBoardRate",
    "ModelEvalBoardRecord",
    "ModelEvalBoardSeries",
    "ModelEvalBoardSizeCell",
    "ModelEvalBoardSizeRow",
    "ModelEvalBoardTested",
]
