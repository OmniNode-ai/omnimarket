# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Inputs and outputs of the cause-ticket-unresolved detector and actor (OMN-22143)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ModelCauseTicketCase(BaseModel):
    """What the detector reads from one cause-ticket-unresolved evidence block."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str  # the queue row's id; '' for a block read outside the queue
    key: str  # the case key, verbatim: merge-sweep-<kind>:{"reasons": [...], "reds": [...], "repo": ...}
    lane_kind: str  # the merge-sweep lane kind the key names, e.g. diagnose
    overlay_key: (
        str  # the lane_dispatch overlay standing_tickets key: merge-sweep-<kind>
    )
    reasons: tuple[str, ...]
    reds: tuple[str, ...]
    repo: str | None
    floor_only: bool  # no reds and every reason is an under-floor reason: no ticket can be cited
    decision: bool


class ModelCauseTicketUnresolvedRequest(BaseModel):
    """One pass of the actor over exception-queue rows.

    ``standing_tickets`` is the lane_dispatch overlay's ``standing_tickets`` map, and ``ticket_states`` the
    tracker status of each ticket it names; a ticket with no status here is unread and resolves nothing.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    state_dir: Path  # the queue lives at <state_dir>/exception-queue
    rows: tuple[dict[str, Any], ...]
    standing_tickets: dict[str, str] = Field(default_factory=dict)
    ticket_states: dict[str, str] = Field(default_factory=dict)
    umbrella_tickets: frozenset[str] = frozenset({"OMN-17427"})
    now: str  # ISO-Z stamp the queue writes


class ModelCauseTicketResolution(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    cls: str  # cause-ticket-unresolved, or '' for a row of another class
    resolved: bool
    ticket: str  # the standing ticket the planner now takes; '' when none
    how: str  # how it was resolved, or what was tried
    emitted: str  # '' or <status>:<queue id>


class ModelCauseTicketUnresolvedResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    resolutions: tuple[ModelCauseTicketResolution, ...]
