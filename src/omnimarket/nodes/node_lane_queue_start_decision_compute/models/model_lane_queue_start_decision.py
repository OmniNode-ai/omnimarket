# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Caller-supplied queue facts and host-specific start decisions.

Stamps are ISO-Z strings. They remain strings so a malformed ledger stamp can
be handled by the decision without rejecting the rest of the queue.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelQueuedLane(BaseModel):
    """One queued lane, including its relaunch and optional host pin history."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str
    model: str
    priority_class: str
    enqueued_at: str
    updated_at: str
    launched_at: str | None = None
    pin: str | None = None
    pin_since: str | None = None
    pin_strict: bool = False
    last_reason: str = ""


class ModelQueueHost(BaseModel):
    """A host's available slots per engine, or the error reading that host."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    claude_slots: int = 0
    codex_slots: int = 0
    probe_error: str | None = None


class ModelWaitingUnit(BaseModel):
    """A runner lane marker or landing worker waiting in a priority class."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    unit: str
    priority_class: str


class ModelLaneQueueStartRequest(BaseModel):
    """All facts required for a decision, including the caller's current time."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    now: str
    priority_classes: list[str]
    entries: list[ModelQueuedLane]
    hosts: list[ModelQueueHost]
    waiting_units: list[ModelWaitingUnit] = Field(default_factory=list)
    relaunch_s: float = 300.0
    pin_s: float = 900.0


class ModelLaneQueueStart(BaseModel):
    """A lane consuming one slot on a host; only an effective pin persists."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str
    engine: Literal["claude", "codex"]
    host: str
    pinned: bool
    reason_code: Literal["start"] = "start"


class ModelLaneQueueHeld(BaseModel):
    """Why a queued lane cannot start in this decision."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str
    reason_code: Literal[
        "relaunch-window",
        "no-free-slot",
        "pinned-host-full",
        "priority-holdback",
        "probe-unread",
    ]
    detail: str


class ModelLaneQueueStartResult(BaseModel):
    """Starts, holds and queue age after keeping the first row for each lane."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    starts: list[ModelLaneQueueStart]
    held: list[ModelLaneQueueHeld]
    depth: int
    oldest_lane: str | None
    oldest_wait_s: int
