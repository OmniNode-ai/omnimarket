# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed facts of the per-class andon threshold (rebuild piece 7, rule 11).

A threshold is the class's measured peak over 24 hour windows. The set of thresholds is
frozen: it carries a hash of its own values, so a value changed by hand without a new hash
is refused when the freeze is read, never silently used.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import date, datetime
from types import MappingProxyType
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

WINDOW_HOURS = 24
PIECE_MIN = 1
PIECE_MAX = 18


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def freeze_hash(window_hours: int, thresholds: Mapping[str, int]) -> str:
    """The hash of a threshold set: sha256 over canonical JSON of the window and the values."""
    body = json.dumps(
        {"window_hours": window_hours, "thresholds": dict(thresholds)},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(body.encode()).hexdigest()


class ModelClassWindowCounts(_Frozen):
    """The census's count of one class in each observed 24 hour window."""

    class_id: str = Field(min_length=1)
    window_counts: tuple[int, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _non_negative(self) -> ModelClassWindowCounts:
        if any(count < 0 for count in self.window_counts):
            raise ValueError(f"class {self.class_id} has a negative window count")
        return self


class ModelMeasurementProvenance(_Frozen):
    """Where a threshold set was measured: the source, the span read and the step between windows."""

    source: str = Field(min_length=1)
    measured_from: datetime
    measured_until: datetime
    step_hours: int = Field(ge=1)
    events_read: int = Field(ge=0)

    @model_validator(mode="after")
    def _span_runs_forward(self) -> ModelMeasurementProvenance:
        if self.measured_until <= self.measured_from:
            raise ValueError("measured_until must be after measured_from")
        return self


class ModelAndonThresholdFreeze(_Frozen):
    """The frozen thresholds: the values, their hash and the date they were last recalibrated."""

    schema_id: Literal["omni.andon-threshold-freeze/1"] = (
        "omni.andon-threshold-freeze/1"
    )
    # Only the 24 hour window: burst scope compares a threshold with a 24 hour count.
    window_hours: int = Field(default=WINDOW_HOURS, ge=WINDOW_HOURS, le=WINDOW_HOURS)
    thresholds: Mapping[str, int] = Field(min_length=1)
    threshold_hash: str = Field(min_length=64, max_length=64)
    recalibrated_on: date
    # Provenance is information about the measurement, not a threshold value: it is outside the hash.
    measurement: ModelMeasurementProvenance | None = None

    @field_validator("thresholds")
    @classmethod
    def _read_only_non_negative(cls, value: Mapping[str, int]) -> Mapping[str, int]:
        """A threshold is a count, never negative, and the frozen set refuses mutation."""
        negative = sorted(name for name, count in value.items() if count < 0)
        if negative:
            raise ValueError(f"negative andon thresholds: {negative}")
        return MappingProxyType(dict(value))

    @model_validator(mode="after")
    def _hash_matches_values(self) -> ModelAndonThresholdFreeze:
        expected = freeze_hash(self.window_hours, self.thresholds)
        if self.threshold_hash != expected:
            raise ValueError(
                "andon threshold values do not match threshold_hash: a changed value "
                "needs a new hash from a recalibration"
            )
        return self


class ModelThresholdComputeRequest(_Frozen):
    classes: tuple[ModelClassWindowCounts, ...] = Field(min_length=1)
    recalibrated_on: date
    window_hours: int = Field(default=WINDOW_HOURS, ge=WINDOW_HOURS, le=WINDOW_HOURS)
    measurement: ModelMeasurementProvenance | None = None

    @model_validator(mode="after")
    def _unique_classes(self) -> ModelThresholdComputeRequest:
        ids = [item.class_id for item in self.classes]
        if len(ids) != len(set(ids)):
            raise ValueError("a class appears twice in the census input")
        return self


class ModelThresholdComputeResult(_Frozen):
    freeze: ModelAndonThresholdFreeze
    overlay_yaml: str


class ModelBurstObservation(_Frozen):
    """The count of one class, in the last 24 hours, that one piece originated."""

    piece: int = Field(ge=PIECE_MIN, le=PIECE_MAX)
    class_id: str = Field(min_length=1)
    count_24h: int = Field(ge=0)


class ModelBurstScopeRequest(_Frozen):
    freeze: ModelAndonThresholdFreeze
    pieces: tuple[int, ...] = Field(min_length=1)
    observations: tuple[ModelBurstObservation, ...] = ()

    @model_validator(mode="after")
    def _observed_pieces_declared(self) -> ModelBurstScopeRequest:
        if len(self.pieces) != len(set(self.pieces)):
            raise ValueError("a piece is listed twice")
        undeclared = {o.piece for o in self.observations} - set(self.pieces)
        if undeclared:
            raise ValueError(
                f"observations name pieces not listed: {sorted(undeclared)}"
            )
        return self


class ModelThresholdBreach(_Frozen):
    class_id: str
    count_24h: int
    threshold: int


class ModelPieceDecision(_Frozen):
    piece: int
    decision: Literal["stopped", "admitted"]
    breaches: tuple[ModelThresholdBreach, ...] = ()


class ModelBurstScopeResult(_Frozen):
    """One decision per listed piece; a stop reaches only the piece that originated the burst."""

    decisions: tuple[ModelPieceDecision, ...]
    stopped_pieces: tuple[int, ...]
    admitted_pieces: tuple[int, ...]
    # Classes seen with no frozen threshold: not judged, never read as within bounds.
    unmeasured_classes: tuple[str, ...] = ()


class ModelClassEvent(_Frozen):
    """One observation of a census class at a time (a friction row's class and its stamp)."""

    class_id: str = Field(min_length=1)
    observed_at: datetime


class ModelWindowMeasureRequest(_Frozen):
    """Rolling 24 hour windows over a span, started every ``step_hours``; the step is the caller's (overlay) setting."""

    events: tuple[ModelClassEvent, ...]
    measured_from: datetime
    measured_until: datetime
    step_hours: int = Field(ge=1)
    window_hours: int = Field(default=WINDOW_HOURS, ge=WINDOW_HOURS, le=WINDOW_HOURS)

    @model_validator(mode="after")
    def _span_holds_a_window(self) -> ModelWindowMeasureRequest:
        if self.measured_from.tzinfo is None or self.measured_until.tzinfo is None:
            raise ValueError("the measured span needs timezone-aware bounds")
        span_hours = (self.measured_until - self.measured_from).total_seconds() / 3600
        if span_hours < self.window_hours:
            raise ValueError(
                f"the span is {span_hours:.1f} hours, shorter than one {self.window_hours} hour window"
            )
        naive = [e.class_id for e in self.events if e.observed_at.tzinfo is None]
        if naive:
            raise ValueError(f"events with no timezone: {sorted(set(naive))[:5]}")
        return self


class ModelWindowMeasureResult(_Frozen):
    """Per class, its count in every window of the span; feeds ``compute_thresholds``."""

    classes: tuple[ModelClassWindowCounts, ...]
    windows_measured: int = Field(ge=1)
    events_read: int = Field(ge=0)
    events_outside_span: int = Field(ge=0)
