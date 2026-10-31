# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed facts the factory floor compute reads: every source is data the caller already read."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnimarket.models.model_host_capacity_advertisement import (
    ModelHostCapacityAdvertisement,
)

ItemState = Literal["done", "active", "prep", "wait", "stop"]
TableTaskState = Literal["merged", "open-green", "open-red", "not-started", "unread"]
PrState = Literal["OPEN", "MERGED", "CLOSED"]


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must carry a UTC offset")
    return value


class ModelFloorManifestTask(BaseModel):
    """One row of a plan's ``.tasks.yaml``; keys the node does not read are ignored."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    id: str = Field(min_length=1)
    title: str = ""
    repo: str = ""
    wave: str = ""
    kind: str = "lane"
    depends_on: tuple[str, ...] = ()
    tickets: tuple[str, ...] = ()
    prs: tuple[str, ...] = ()


class ModelFloorManifest(BaseModel):
    """The task manifest beside a plan."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    title: str = ""
    epic: str = ""
    tasks: tuple[ModelFloorManifestTask, ...]


class ModelFloorTableTask(BaseModel):
    """One row of the plans-today task table, the fallback when a plan has no manifest."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    label: str = Field(min_length=1)
    text: str = ""
    tickets: tuple[str, ...] = ()
    prs: tuple[str, ...] = ()
    state: TableTaskState
    state_basis: str = ""
    owner: str = ""


class ModelFloorPlanInput(BaseModel):
    """One plan: its identity, its manifest when it has one, its plans-today table."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    plan_path: str = Field(min_length=1)
    title: str = ""
    tickets: tuple[str, ...] = ()
    prs: tuple[str, ...] = ()
    manifest: ModelFloorManifest | None = None
    table_tasks: tuple[ModelFloorTableTask, ...] = ()


class ModelFloorPr(BaseModel):
    """One pull request as the PR watcher records it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str = Field(min_length=1)
    state: PrState
    draft: bool = False
    red: bool = False
    title: str = ""
    head_ref: str = ""
    tickets: tuple[str, ...] = ()


class ModelFloorMerge(BaseModel):
    """One merge, from the watcher's merge record (the landing controller's merges among them)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str = Field(min_length=1)
    merged_at: datetime
    title: str = ""
    head_ref: str = ""
    tickets: tuple[str, ...] = ()

    _utc = field_validator("merged_at")(_aware)


class ModelFloorWatcher(BaseModel):
    """The PR watcher state: open and merged pull requests and the merge record."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    last_tick: datetime
    prs: tuple[ModelFloorPr, ...] = ()
    merges: tuple[ModelFloorMerge, ...] = ()

    _utc = field_validator("last_tick")(_aware)


class ModelFloorLedgerRow(BaseModel):
    """The cells of one ledger row the floor reads (CLAIM, TERMINAL, HOLD, RELEASE, gate STATUS)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ts: datetime
    kind: str = Field(min_length=1)
    lane: str = ""
    tickets: tuple[str, ...] = ()
    prs: tuple[str, ...] = ()
    row_id: str = ""
    re: str = ""
    until: datetime | None = None
    reason: str = ""
    task: str = ""
    state: str = ""
    subject: str = ""
    verdict: str = ""
    outcome: str = ""
    closes_claim: str = ""
    text: str = ""

    _utc = field_validator("ts")(_aware)


class ModelFloorLinearIssue(BaseModel):
    """One issue of the Linear snapshot plans-today writes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    status: str = ""
    status_type: str = ""


class ModelFloorStoppage(BaseModel):
    """One friction class from the friction report."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    occurrences: int = Field(ge=0)
    cost_minutes: float = Field(ge=0.0)
    recurring: bool = False
    tickets: tuple[str, ...] = ()
    last_seen: str = ""


class ModelFloorSelection(BaseModel):
    """Which plans are lines: a plan whose path contains any text; empty selects every plan given."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    plan_path_contains: tuple[str, ...] = ()


class ModelFactoryFloorConfig(BaseModel):
    """The vocabularies and limits the contract's ``config`` block declares; the handler holds none."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_id: str = Field(min_length=1)
    andon_markers: tuple[str, ...] = Field(min_length=1)
    gate_kind: str = Field(min_length=1)
    gate_state: str = Field(min_length=1)
    gate_fail_verdicts: tuple[str, ...] = Field(min_length=1)
    gate_pass_verdicts: tuple[str, ...] = Field(min_length=1)
    done_outcomes: tuple[str, ...] = Field(min_length=1)
    blocked_outcomes: tuple[str, ...] = Field(min_length=1)
    live_claim_minutes: int = Field(gt=0)
    advertisement_stale_factor: int = Field(gt=0)
    station_name_template: str = Field(min_length=1)
    fallback_station_id: str = Field(min_length=1)
    fallback_station_name: str = Field(min_length=1)
    shipped_window_hours: int = Field(gt=0)
    shipped_limit: int = Field(gt=0)
    waiting_limit: int = Field(gt=0)
    stoppage_limit: int = Field(gt=0)
    note_chars: int = Field(gt=20)


class ModelFactoryFloorRequest(BaseModel):
    """Everything one run reads. The node has no clock: ``generated_at`` is a fact the caller supplies."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    generated_at: datetime
    config: ModelFactoryFloorConfig
    selection: ModelFloorSelection = Field(default_factory=ModelFloorSelection)
    plans: tuple[ModelFloorPlanInput, ...]
    watcher: ModelFloorWatcher
    ledger: tuple[ModelFloorLedgerRow, ...] = ()
    linear: tuple[ModelFloorLinearIssue, ...] = ()
    placement: tuple[ModelHostCapacityAdvertisement, ...] = ()
    stoppages: tuple[ModelFloorStoppage, ...] = ()

    _utc = field_validator("generated_at")(_aware)
