# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models of the merged-and-running count: the facts a reader supplies, the declared config, the counts.

The count is of merges, never of hand-offs: a pull request counts only while its state is one of the
config's ``counted_pr_states`` and it merged to one of the config's ``landing_branches``. A merge is
running only when a piece of evidence of a declared kind names it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("a timestamp must carry a timezone")
    return value


def _aware_optional(value: datetime | None) -> datetime | None:
    return None if value is None else _aware(value)


class ModelMrcEvidenceSource(BaseModel):
    """One kind of running-on-dev evidence, the repositories it is recorded for, and what its absence means."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str = Field(min_length=1)
    repos: tuple[str, ...]
    missing_reason: str = Field(min_length=1)


class ModelMrcConfig(BaseModel):
    """Every threshold, label, window, timezone and vocabulary of the count, declared by the contract or an overlay."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_id: str = Field(min_length=1)
    operator_timezone: str = Field(min_length=1)
    window_days: int = Field(gt=0)
    landing_branches: tuple[str, ...] = Field(min_length=1)
    counted_pr_states: tuple[str, ...] = Field(min_length=1)
    sha_match_chars: int = Field(gt=0)
    evidence_sources: tuple[ModelMrcEvidenceSource, ...]
    no_evidence_source_reason: str = Field(min_length=1)

    @field_validator("operator_timezone")
    @classmethod
    def _known_zone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone {value!r}") from exc
        return value

    @model_validator(mode="after")
    def _distinct_kinds(self) -> ModelMrcConfig:
        kinds = [source.kind for source in self.evidence_sources]
        if len(set(kinds)) != len(kinds):
            raise ValueError("evidence_sources repeats a kind")
        return self


class ModelMrcPr(BaseModel):
    """One pull request as a merge source read it. Only merged states with a merge time are counted."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(min_length=1)
    number: int = Field(gt=0)
    state: str = Field(min_length=1)
    base_branch: str = ""
    merge_sha: str = ""
    merged_at: datetime | None = None

    _utc = field_validator("merged_at")(_aware_optional)


class ModelMrcEvidence(BaseModel):
    """One record that a merge runs on dev: an adoption row, a deploy receipt. ``number`` or ``merge_sha`` names the merge."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    source: str = Field(min_length=1)
    number: int | None = Field(default=None, gt=0)
    merge_sha: str = ""
    observed_at: datetime

    _utc = field_validator("observed_at")(_aware)

    @model_validator(mode="after")
    def _names_a_merge(self) -> ModelMrcEvidence:
        if self.number is None and not self.merge_sha:
            raise ValueError(
                "evidence names neither a pull request number nor a merge sha"
            )
        return self


class ModelMrcHost(BaseModel):
    """One host of the declared host table with the slots the placement read gave it (None: not read)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str = Field(min_length=1)
    slots: int | None = Field(default=None, ge=0)
    available: bool = True


class ModelMrcLiveLane(BaseModel):
    """One live lane and the slots it holds on its host."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str = Field(min_length=1)
    host: str = Field(min_length=1)
    slots: int = Field(default=1, ge=1)


class ModelMergedRunningCountRequest(BaseModel):
    """Everything one run reads. The node has no clock: ``now`` is a fact the caller supplies."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    now: datetime
    config: ModelMrcConfig
    prs: tuple[ModelMrcPr, ...]
    evidence: tuple[ModelMrcEvidence, ...] = ()
    hosts: tuple[ModelMrcHost, ...]
    live_lanes: tuple[ModelMrcLiveLane, ...] = ()

    _utc = field_validator("now")(_aware)


class ModelMrcMergeVerdict(BaseModel):
    """One counted merge: whether it runs, on what evidence, or why it is not shown running."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str
    number: int
    merge_sha: str
    merged_at: datetime
    utc_date: str
    local_date: str
    running: bool
    evidence_kind: str
    evidence_source: str
    reason: str


class ModelMrcRepoCount(BaseModel):
    """One repository's counts on one day."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str
    merged: int
    running: int
    not_running: int


class ModelMrcDay(BaseModel):
    """One operator-timezone calendar day, with the UTC span it covers."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    local_date: str
    utc_start: datetime
    utc_end: datetime
    merged: int
    running: int
    not_running: int
    by_repo: tuple[ModelMrcRepoCount, ...]
    merges: tuple[ModelMrcMergeVerdict, ...]


class ModelMrcHostIdle(BaseModel):
    """One host's slots against the live lanes' slots. ``unread``: no placement read; ``out``: the table marks it unavailable."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str
    state: Literal["read", "unread", "out"]
    slots: int | None
    live_slots: int
    idle: int | None


class ModelMergedRunningCountResult(BaseModel):
    """Merged, merged-and-running and merged-not-running per day, and idle slots per host. No hand-off count."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_id: str
    generated_at: datetime
    operator_timezone: str
    window_days: int
    days: tuple[ModelMrcDay, ...]
    idle_hosts: tuple[ModelMrcHostIdle, ...]
    idle_total: int
    lanes_on_unlisted_hosts: tuple[str, ...]
    ignored_evidence: int
