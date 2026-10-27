# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Facts in and measures out of node_flow_measures_compute (OMN-21441).

Every repository name, every basis and every time arrives in a request; the node
holds none of them. A source names the repositories it read, so a repository no
source read prints ``unread`` and a repository a source read with nothing to count
prints a real zero.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class EnumFlowMeasure(StrEnum):
    MERGES_PER_HOUR = "merges_per_hour"
    GREEN_TO_MERGED_MEDIAN = "green_to_merged_median"
    PRS_HELD = "prs_held"
    STOP_TIMES = "stop_times"
    FILL_COUNTS = "fill_counts"


class EnumHoldReason(StrEnum):
    WIP_OVER_LIMIT = "wip_over_limit"
    HELD_FOR_PERSON = "held_for_person"
    HELD_BY_CONTROLLER = "held_by_controller"


class EnumFillEvent(StrEnum):
    WIP_DIVERTED = "wip_diverted"
    WIP_IDLE_OVERRIDE = "wip_idle_override"


class ModelFlowMeasuresConfig(BaseModel):
    """The ``config.wip_gate`` block of the contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    trailing_days: int = Field(ge=1)


class _ModelSource(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    read_at: AwareDatetime
    basis: str = Field(min_length=1)


class _ModelRepositorySource(_ModelSource):
    repositories_read: tuple[str, ...]


class ModelMergeFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str
    merged_at: AwareDatetime
    green_at: AwareDatetime | None = None


class ModelMergeSource(_ModelRepositorySource):
    merges: tuple[ModelMergeFact, ...] = ()

    @model_validator(mode="after")
    def _merges_name_a_repository_read(self) -> Self:
        stray = {m.repository for m in self.merges} - set(self.repositories_read)
        if stray:
            raise ValueError(f"merges name unread repositories: {sorted(stray)}")
        return self


class ModelHeldFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str
    reason: EnumHoldReason


class ModelHeldSource(_ModelRepositorySource):
    held: tuple[ModelHeldFact, ...] = ()

    @model_validator(mode="after")
    def _held_name_a_repository_read(self) -> Self:
        stray = {h.repository for h in self.held} - set(self.repositories_read)
        if stray:
            raise ValueError(f"held PRs name unread repositories: {sorted(stray)}")
        return self


class ModelStopRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    detected_at: AwareDatetime
    stopped_at: AwareDatetime | None = None
    fixed_at: AwareDatetime | None = None


class ModelStopSource(_ModelSource):
    records: tuple[ModelStopRecord, ...] = ()


class ModelFillEventFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str
    event: EnumFillEvent
    at: AwareDatetime


class ModelFillSource(_ModelRepositorySource):
    events: tuple[ModelFillEventFact, ...] = ()

    @model_validator(mode="after")
    def _events_name_a_repository_read(self) -> Self:
        stray = {e.repository for e in self.events} - set(self.repositories_read)
        if stray:
            raise ValueError(f"fill events name unread repositories: {sorted(stray)}")
        return self


class ModelFlowMeasuresRequest(BaseModel):
    """Facts and ``now``. A source left out is a source nobody read."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    now: AwareDatetime
    repositories: tuple[str, ...] = ()
    merges: ModelMergeSource | None = None
    held: ModelHeldSource | None = None
    stops: ModelStopSource | None = None
    fill: ModelFillSource | None = None


class ModelFlowMeasure(BaseModel):
    """One printed reading: its value (``None`` is ``unread``), read time and basis."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    measure: EnumFlowMeasure
    subject: str
    label: str
    value: float | None
    display: str
    read_at: AwareDatetime | None
    basis: str
    line: str


class ModelFlowMeasuresResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    now: AwareDatetime
    trailing_days: int
    measures: tuple[ModelFlowMeasure, ...]
    lines: tuple[str, ...]
