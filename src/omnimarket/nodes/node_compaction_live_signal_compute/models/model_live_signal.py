# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed inputs and results for the live compaction signal readers.

The caller owns every read: ledger rows, operator prompts and boundary windows
arrive as typed records, so the node performs no file, bus or model access.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

Count = Annotated[int, Field(strict=True, ge=0)]
Nonempty = Annotated[str, Field(min_length=1)]


class ModelBoundaryWindow(BaseModel):
    """One session window: from a compaction boundary to the next boundary or the session end."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    window_id: Nonempty
    session_id: Nonempty
    start: AwareDatetime
    end: AwareDatetime

    @model_validator(mode="after")
    def ordered(self) -> ModelBoundaryWindow:
        if self.end <= self.start:
            raise ValueError(f"Window {self.window_id} must end after it starts")
        return self


class ModelGuardRefusalRow(BaseModel):
    """A FRICTION ledger row; only `guard == "question_guard"` rows are counted."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    row_id: Nonempty
    timestamp: AwareDatetime
    guard: str
    session_id: str | None = None


class ModelOperatorPrompt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    prompt_id: Nonempty
    session_id: Nonempty
    timestamp: AwareDatetime
    text: str


class ModelRulingRow(BaseModel):
    """A RULING ledger row: its stamp, the operator's verbatim quote and the stamps it amends."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    stamp: AwareDatetime
    quote: str
    amends: tuple[AwareDatetime, ...] = ()


class ModelLiveSignalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    windows: tuple[ModelBoundaryWindow, ...]
    guard_rows: tuple[ModelGuardRefusalRow, ...] = ()
    prompts: tuple[ModelOperatorPrompt, ...] = ()
    rulings: tuple[ModelRulingRow, ...] = ()
    shingle_size: Annotated[int, Field(strict=True, ge=1)] = 5
    restatement_overlap: Annotated[float, Field(gt=0, le=1)] = 0.8
    amend_window_seconds: Annotated[int, Field(strict=True, ge=0)] = 600

    @model_validator(mode="after")
    def unique_ids(self) -> ModelLiveSignalRequest:
        for label, ids in (
            ("window_id", [w.window_id for w in self.windows]),
            ("row_id", [r.row_id for r in self.guard_rows]),
            ("prompt_id", [p.prompt_id for p in self.prompts]),
        ):
            if len(ids) != len(set(ids)):
                raise ValueError(f"Duplicate {label}")
        return self


class ModelRestatement(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    prompt_id: str
    matched_ruling_stamps: tuple[AwareDatetime, ...]


class ModelWindowSignal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    window_id: str
    session_id: str
    start: AwareDatetime
    end: AwareDatetime
    duration_hours: float
    refusal_row_ids: tuple[str, ...]
    refusal_count: Count
    refusals_per_hour: float
    restatements: tuple[ModelRestatement, ...]
    restatement_count: Count
    restatements_per_hour: float


class ModelUnassignedRefusal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    row_id: str
    reason: Literal["outside_windows", "ambiguous_windows"]
    candidate_window_ids: tuple[str, ...] = ()


class ModelLiveSignalResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    windows: tuple[ModelWindowSignal, ...]
    question_guard_rows: Count
    assigned_rows: Count
    unassigned: tuple[ModelUnassignedRefusal, ...]
    ignored_other_guard_rows: Count
