# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Requests and results of the lab-fill planner (OMN-20668).

Result keys keep the camelCase names the lab-fill workflow reads today, so the
workflow and this node answer the same shape until the workflow is retired.
Optional keys are set only when the decision has them.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from .model_lab_fill_plan_config import (
    ModelLabFillHeadroomPolicy,
    ModelLabFillPlanConfig,
)

_CAMEL = ConfigDict(
    frozen=True, extra="forbid", alias_generator=to_camel, populate_by_name=True
)


class ModelLabFillHostCapacity(BaseModel):
    """One lab host's lanes for this run and why it has none."""

    model_config = _CAMEL

    name: str
    lanes: int
    idle_lanes: int
    runner_slots: int
    cap_bound: bool
    codex: bool
    claude: bool
    running: int
    load_per_core: float | None
    reason: str


class ModelLabFillCapacityRequest(BaseModel):
    """Host readings from the runner's placement read, and the headroom policy."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    readings: tuple[object, ...]
    policy: ModelLabFillHeadroomPolicy
    max_lanes: int = 8


class ModelLabFillCapacityResult(BaseModel):
    """Lanes free on every lab host and the run's lane budget."""

    model_config = _CAMEL

    hosts: tuple[ModelLabFillHostCapacity, ...]
    free: int
    budget: int


class ModelLabFillFallback(BaseModel):
    """The engine a lane moved off, and why."""

    model_config = _CAMEL

    from_engine: str = Field(alias="from")
    reason: str


class ModelLabFillDispatchItem(BaseModel):
    """One lane to dispatch."""

    model_config = _CAMEL

    lane: str
    kind: str
    id: str | None = None
    ticket: str
    pr: str
    repo: str
    repo_source: str | None = None
    ref: str
    title: str
    updated_at: str
    host: str
    pinned: bool
    engine: str
    route: str
    fallback: ModelLabFillFallback | None = None


class ModelLabFillSkipEntry(BaseModel):
    """A candidate not dispatched, with the reason."""

    model_config = _CAMEL

    id: str
    kind: object | None = None
    reason: str
    host: str | None = None


class ModelLabFillDispatchPlanRequest(BaseModel):
    """Candidates, the capacity plan, the run configuration and the fenced tickets."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    candidates: tuple[object, ...]
    capacity: ModelLabFillCapacityResult
    config: ModelLabFillPlanConfig
    fenced: tuple[str, ...] = ()


class ModelLabFillDispatchPlanResult(BaseModel):
    """Lanes to dispatch, candidates skipped, candidates deferred."""

    model_config = _CAMEL

    dispatch: tuple[ModelLabFillDispatchItem, ...]
    skipped: tuple[ModelLabFillSkipEntry, ...]
    deferred: tuple[ModelLabFillSkipEntry, ...]
    budget: int


class ModelLabFillCandidateChoiceRequest(BaseModel):
    """The live enumerate read and the cache-only read, either of which may be absent."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    enumerated: Mapping[str, object] | None = None
    cached: Mapping[str, object] | None = None
    config: ModelLabFillPlanConfig


class ModelLabFillCandidateChoiceResult(BaseModel):
    """Which read served the tick and what it carried."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    source: str
    candidates: tuple[object, ...]
    fenced: tuple[str, ...]
    detail: str
    filtered: object | None = None
    open_count_gate: object | None = None
    source_diagnostics: object | None = None
    prefiltered_skipped: tuple[object, ...] | None = Field(
        default=None, alias="prefilteredSkipped"
    )
    not_emitted: object | None = None
