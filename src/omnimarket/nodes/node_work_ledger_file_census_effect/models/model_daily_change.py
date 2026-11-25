# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed models of the census's daily publication: the day-over-day change and the one event the daily run puts on the bus.

The change is read over the repositories counted on both days. A repository absent or unreadable on either day, or not
registered on the other day, is named in ``skipped_repositories`` and never diffed, and the first run with no previous
record states that in ``basis`` and carries no change: nothing here is a fabricated figure.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_work_ledger_file_census_effect.models.model_file_census import (
    ModelCensusDailyRecord,
)

EVENT_TYPE = "ledger_file_census.daily_record"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EnumCensusChangeBasis(StrEnum):
    PREVIOUS_DAY = "previous-day"
    NO_PREVIOUS_RECORD = "no-previous-record"
    HISTORY_UNREADABLE = "history-unreadable"


class ModelCensusRepositoryChange(_Frozen):
    repository: str
    files_before: int = Field(ge=0)
    files_after: int = Field(ge=0)
    files_change: int
    runtime_files_before: int = Field(ge=0)
    runtime_files_after: int = Field(ge=0)
    runtime_files_change: int


class ModelCensusDailyChange(_Frozen):
    """The change against the previous day's record; a negative figure is a shrink."""

    basis: EnumCensusChangeBasis
    previous_recorded_on: str | None = None
    compared_repositories: tuple[str, ...] = ()
    skipped_repositories: tuple[str, ...] = ()
    total_files_change: int | None = Field(
        default=None,
        description="Files counted today less files counted the previous day, over the compared repositories that are not "
        "on their own line; None when there is no previous day to read.",
    )
    runtime_files_change: int | None = None
    repositories: tuple[ModelCensusRepositoryChange, ...] = ()


class ModelCensusDailyEventRequest(_Frozen):
    record: ModelCensusDailyRecord
    previous: ModelCensusDailyRecord | None = None
    host: str = Field(min_length=1)
    previous_unreadable: bool = False


class ModelCensusDailyEvent(_Frozen):
    """The one event of a daily run: the record, whether it is partial, and the change against the previous day."""

    host: str
    record: ModelCensusDailyRecord
    partial: bool = Field(
        description="A registered repository was absent or unreadable, so the counts are of the others only."
    )
    absent_repositories: tuple[str, ...]
    change: ModelCensusDailyChange


class ModelCensusDailyPublicationRequest(_Frozen):
    record: ModelCensusDailyRecord


class ModelCensusDailyPublicationResult(_Frozen):
    event: ModelCensusDailyEvent
    published: bool
    history_kept: bool
