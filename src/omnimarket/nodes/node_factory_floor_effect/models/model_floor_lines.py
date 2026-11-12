# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ``lines`` operation: its request, its result, its contract block and the published record as it reads it.

The record model reads the JSON the factory floor effect published and nothing else: it keeps only the fields the
board prints, ignores the rest, and gives every field the line board adds after the base a default that means
"the record does not carry it", so a record from before those fields renders as unread, never as clean.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise ValueError("a time must carry a UTC offset")
    return value


class ModelFloorLinesRequest(BaseModel):
    """The flags of one ``lines`` run.

    ``record_path`` names a record file; ``history_at`` asks for the newest history snapshot at or before that time;
    with neither, the newest record the effect wrote under the registry home is read. ``now`` ``None`` reads the clock.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    record_path: str | None = None
    history_at: datetime | None = None
    now: datetime | None = None
    home: str | None = None
    history_dir: str | None = None

    @field_validator("history_at", "now")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        return _aware(value)


class ModelFloorLinesResult(BaseModel):
    """The board text, the time of the record it came from and the exit status (non-zero when the record is unread)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    board: str
    record_time: str
    exit_status: int


class ModelFloorLinesWords(BaseModel):
    """The board's fixed words."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    unread_record: str = Field(min_length=1)
    unread_absent: str = Field(min_length=1)
    unread_unparsable: str = Field(min_length=1)
    unread_not_in_record: str = Field(min_length=1)
    stops_not_in_record: str = Field(min_length=1)
    no_stops: str = Field(min_length=1)
    cause_not_recorded: str = Field(min_length=1)
    cut_mark: str = Field(min_length=1)


class ModelFloorLinesConfig(BaseModel):
    """The contract's ``lines`` block: every limit, name and word the board prints comes from here."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # A record older than this reads stale: the board prints UNREAD first and the exit status is non-zero.
    record_max_age_min: int = Field(gt=0)
    # The eight sources the board depends on, in print order; each is a ``sources.health[].source`` name.
    health_sources: tuple[str, ...] = Field(min_length=1)
    # The line stage names, in print order.
    stage_order: tuple[str, ...] = Field(min_length=1)
    # The variable that names the directory holding the history snapshots, and the pieces of a snapshot's file name.
    history_dir_env: str = Field(min_length=1)
    history_subdirectory: str = Field(min_length=1)
    history_timestamp_format: str = Field(min_length=1)
    # Longest sentence or cause printed, in characters; longer text is cut and marked.
    sentence_chars: int = Field(gt=0)
    unread_exit_status: int = Field(gt=0)
    # The words the board prints (kept here so no word is a literal in the renderer).
    words: ModelFloorLinesWords


class ModelFloorLinesContract(BaseModel):
    """The ``lines`` block of contract.yaml; the rest of the contract is read by the other operations."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    lines: ModelFloorLinesConfig


class ModelRecordHealth(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    source: str
    state: str
    read_at: str | None = None
    cause: str = ""


class ModelRecordEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    kind: str = ""
    ref: str = ""


class ModelRecordStop(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    kind: str
    sentence: str = ""
    since: str | None = None
    evidence: tuple[ModelRecordEvidence, ...] = ()
    cause: str = ""


class ModelRecordAndon(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    flag: bool = False
    reason: str = ""


class ModelRecordLine(BaseModel):
    """One line as the board prints it. ``stops`` ``None`` means the record carries no stops field at all."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    line_id: str
    name: str = ""
    condition: str = ""
    andon: ModelRecordAndon = ModelRecordAndon()
    stops: tuple[ModelRecordStop, ...] | None = None
    stage_counts: dict[str, int] | None = None
    owner: str = ""
    ruled_by: tuple[str, ...] = ()


class ModelRecordSources(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    health: tuple[ModelRecordHealth, ...] = ()


class ModelFloorLinesRecord(BaseModel):
    """The published factory floor record, as far as the board reads it."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    generated_at: datetime
    lines: tuple[ModelRecordLine, ...] = ()
    sources: ModelRecordSources = ModelRecordSources()

    @field_validator("generated_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        result = _aware(value)
        assert result is not None
        return result
