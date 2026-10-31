# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The factory floor record: one line per selected plan and the floor section."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_factory_floor_compute.models.model_factory_floor_input import (
    ItemState,
)

StatusPill = Literal["stopped", "done", "building", "queued"]
LineSource = Literal["manifest", "plan-task-table"]


class ModelFloorItem(BaseModel):
    """One part (a task) at a station."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    title: str
    kind: str
    state: ItemState
    note: str
    tickets: tuple[str, ...]
    prs: tuple[str, ...]


class ModelFloorStation(BaseModel):
    """One station: a wave or stage of a plan."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    name: str
    state: ItemState
    items: tuple[ModelFloorItem, ...]
    feeders: tuple[str, ...]


class ModelFloorAndon(BaseModel):
    """The andon: an open andon hold or a failed gate stops the line."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    flag: bool
    reason: str
    causes: tuple[str, ...]


class ModelFloorLine(BaseModel):
    """One car on the line: a selected plan."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    line_id: str
    name: str
    plan_path: str
    source: LineSource
    status_pill: StatusPill
    percent_built: int
    stations: tuple[ModelFloorStation, ...]
    andon: ModelFloorAndon


class ModelFloorBay(BaseModel):
    """One lab host's bays: lanes running, bays free, and whether its advertisement is stale."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str
    slots: int
    running: int
    free: int
    stale: bool


class ModelFloorPrRef(BaseModel):
    """A pull request on the floor, shipped (merged) or waiting, and the line it belongs to."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pr: str
    title: str
    line_id: str
    detail: str


class ModelFloorStoppageOut(BaseModel):
    """A friction class standing on the floor."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    occurrences: int
    cost_minutes: float
    recurring: bool
    tickets: tuple[str, ...]
    last_seen: str


class ModelFloorSection(BaseModel):
    """The floor around the lines."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    bays: tuple[ModelFloorBay, ...]
    shipped: tuple[ModelFloorPrRef, ...]
    waiting: tuple[ModelFloorPrRef, ...]
    stoppages: tuple[ModelFloorStoppageOut, ...]


class ModelFloorSources(BaseModel):
    """What the record was computed from, so a reader can judge its freshness."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    watcher_last_tick: str
    ledger_rows: int
    ledger_newest_row: str
    linear_issues: int
    placement_hosts: int
    stoppage_report: bool


class ModelFactoryFloorResult(BaseModel):
    """The factory floor record."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_id: str
    generated_at: str
    lines: tuple[ModelFloorLine, ...]
    floor: ModelFloorSection
    sources: ModelFloorSources
