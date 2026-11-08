# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Facts and vocabulary of the factory floor's line selection: the six line classes and their coverage.

A line is one of six classes (lean line board plan, section 4). The caller supplies the facts each class is chosen
from, with the health of the source each came from; the contract's config block supplies the class vocabulary.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

LineClass = Literal["declared", "plan", "goal", "ruling", "ticket", "process"]
HealthState = Literal["read", "read-empty", "stale", "unread"]


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise ValueError("timestamps must carry a UTC offset")
    return value


class ModelFloorGoalRow(BaseModel):
    """One row of the goal file; only an open row is a line (class goal), keyed ``goal:<row_id>``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    row_id: str = Field(min_length=1)
    name: str = ""
    is_open: bool = True
    tickets: tuple[str, ...] = ()
    verdict: str = ""
    next_action: str = ""
    probe: str = ""


class ModelFloorProcessDecl(BaseModel):
    """A factory process the deployment overlay declares; each is a line (class process), keyed ``process:<name>``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    title: str = ""
    actor: str = ""
    # The ledger row kind and cell match that is one run of the process, and the match that says that run did nothing.
    run_kind: str = ""
    run_match: str = ""
    did_nothing_match: str = ""
    interval_min: int = Field(default=0, ge=0)
    # The outcomes (``success``, ``did-nothing``, ``error``) the process's own contract says its run record covers. A
    # process that does not cover all three is a process whose silence is not detectable.
    outcomes_covered: tuple[str, ...] = ()


class ModelFloorSourceHealth(BaseModel):
    """What the caller found when it read one source: read, read-empty, stale or unread, when, and why."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: str = Field(min_length=1)
    state: HealthState
    read_at: datetime | None = None
    cause: str = ""

    _utc = field_validator("read_at")(_aware)


class ModelFloorClassRule(BaseModel):
    """One line class: its id prefix, its label in the coverage notes and the sources its lines are selected from."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    line_class: LineClass
    # ``goal`` makes ``goal:<id>``; empty for the two plan classes, whose id is the plan file's base name.
    id_prefix: str = ""
    label: str = Field(min_length=1)
    sources: tuple[str, ...] = ()


class ModelFloorLineSelectConfig(BaseModel):
    """The selection vocabulary the contract declares; the compute holds none of it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # The classes in their order: a line in two classes is one line, at the lowest class number.
    classes: tuple[ModelFloorClassRule, ...] = Field(min_length=6, max_length=6)
    selection_window_days: int = Field(gt=0)
    # Linear statuses whose ticket is work in progress, and status types that close a ticket.
    in_progress_statuses: tuple[str, ...] = Field(min_length=1)
    closed_status_types: tuple[str, ...] = Field(min_length=1)
    claim_kind: str = Field(min_length=1)
    terminal_kind: str = Field(min_length=1)
    ruling_kind: str = Field(min_length=1)
    # {label} is the class's label, {read_at} the time of the source read.
    stale_note: str = Field(min_length=1)
    unread_note: str = Field(min_length=1)
    missing_health_cause: str = Field(min_length=1)
    bare_progress_note: str = Field(min_length=1)
    ticket_line_note: str = Field(min_length=1)
