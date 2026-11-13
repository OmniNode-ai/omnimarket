# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request and result of the dispatch reading (OMN-22963).

The request carries facts a fetching effect already read (the lanes running or started in the window and the
dispatch records with their terminal results) and the settings from the overlay. The result is one verdict per
subject and one overall verdict. Nothing here reads a clock or a network.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.model_reading import EnumReadingVerdict


class EnumDispatchReadFailure(StrEnum):
    """Why the fetching effect could not read the lanes or the records; every one reads UNKNOWN."""

    LANES_UNREADABLE = "lanes-unreadable"
    RECORDS_UNREADABLE = "records-unreadable"
    BUDGET_EXHAUSTED = "budget-exhausted"


class ModelLaneFact(BaseModel):
    """One lane seen in the window and the command or script path that started it, when the read carried it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    started_at: datetime
    launched_from: str | None = None


class ModelTerminalResult(BaseModel):
    """The terminal result a dispatch reconciled to: when it arrived and what it said."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    at: datetime
    outcome: str = Field(min_length=1)


class ModelDispatchRecord(BaseModel):
    """One dispatch record: the lane it dispatched, its deadline and its terminal result, if one arrived."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dispatch_id: str = Field(min_length=1)
    lane: str = Field(min_length=1)
    dispatched_at: datetime
    deadline_at: datetime
    terminal: ModelTerminalResult | None = None


class ModelDispatchReadingSettings(BaseModel):
    """The overlay's settings; no default here is a policy."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scratch_script_markers: tuple[str, ...] = Field(min_length=1)
    max_age_seconds: int = Field(gt=0)


class ModelDispatchReadingRequest(BaseModel):
    """The facts of one read, the settings, and the time it is judged at.

    ``lanes`` and ``records`` are None when that side was not read.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    now: datetime
    read_at: datetime
    settings: ModelDispatchReadingSettings
    lanes: tuple[ModelLaneFact, ...] | None = None
    records: tuple[ModelDispatchRecord, ...] | None = None
    read_failure: EnumDispatchReadFailure | None = None


class ModelReadingSubject(BaseModel):
    """One subject of the reading: its verdict, its count and the reason in words."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    verdict: EnumReadingVerdict
    count: int | None = None
    reason: str


class ModelNamedLane(BaseModel):
    """A lane the reading names because it has no dispatch record, and whether a scratch script started it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str
    kind: str
    launched_from: str | None = None
    from_scratch_script: bool


class ModelUnreconciledDispatch(BaseModel):
    """A dispatch record that did not reconcile, and why."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dispatch_id: str
    lane: str
    reason: str


class ModelDispatchReadingResult(BaseModel):
    """The reading: one overall verdict, the subjects behind it, and the lanes and dispatches it names."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    verdict: EnumReadingVerdict
    subjects: tuple[ModelReadingSubject, ...]
    lanes_read: int
    records_read: int
    lanes_without_record: tuple[ModelNamedLane, ...] = ()
    unreconciled: tuple[ModelUnreconciledDispatch, ...] = ()
    read_failure: EnumDispatchReadFailure | None = None
