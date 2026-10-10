# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request and result for the merge-throughput tick decisions (OMN-20686)."""

from __future__ import annotations

from typing import Literal

from omnibase_core.types import JsonType
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.lab_fill import ModelLabFillPrLandFacts, ModelLabFillPrLandPlan

_TS = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"


class ModelLabMark(BaseModel):
    """A limited or auth-expired mark file the caller read in the placement directory.

    `absent`: no such file. `unreadable`: the file exists but could not be read or lacks its
    keys. `present`: `until` (and `at` for an auth mark) hold the stamps the file carries.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: Literal["absent", "present", "unreadable"] = "absent"
    until: str | None = None
    at: str | None = None


class ModelLabReceipt(BaseModel):
    """One remote-lane runner receipt the caller read, reduced to the fields the finding uses."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    started_at: str
    host: str | None = None
    final: bool = False
    status: str | None = None
    pid_alive: bool | None = Field(
        default=None,
        description="Whether the receipt's pid is a live process; None when the receipt has no pid.",
    )
    pid_valid: bool = Field(
        default=True,
        description="False when the receipt's pid is not an integer; such a running receipt is skipped.",
    )
    readings: list[str] = Field(default_factory=list)
    lane: str | None = Field(
        default=None, description="The lane the receipt dispatched (OMN-20840)."
    )
    run_id: str | None = None
    brief: str | None = Field(
        default=None,
        description="The brief path the lane ran; a later receipt of the same brief supersedes a failed one.",
    )
    reason: str | None = Field(
        default=None,
        description="Why the lane ended as it did, as the receipt records it (refusal, failure or error).",
    )
    path: str | None = Field(
        default=None, description="Where the caller read the receipt."
    )


class ModelOpenPoint(BaseModel):
    """The open PR count one earlier tick run recorded (OMN-20840)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    at: str = Field(pattern=_TS)
    open: int = Field(ge=0)


class ModelLabReadingParse(BaseModel):
    """What the placement module made of one placement reading text for one host."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    parsed: bool
    admission_refusal: str | None = None


class ModelLabHost(BaseModel):
    """A pool host the caller read, with the marks, parse and admission facts for it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    local: bool = False
    cap: int | None = None
    limited_mark: ModelLabMark = Field(default_factory=ModelLabMark)
    auth_mark: ModelLabMark = Field(default_factory=ModelLabMark)
    parses: dict[str, ModelLabReadingParse] = Field(
        default_factory=dict,
        description=(
            "For each placement reading text of this host in the receipts, whether the placement "
            "module parsed it and the lane admission refusal it names, if any."
        ),
    )


class ModelLabHeadroomFacts(BaseModel):
    """The remote-lane runner's pool, marks and receipts as the caller read them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    module_unavailable: bool = False
    unavailable_hosts: list[str] = Field(
        default_factory=list,
        description="Names of non-local host-table rows marked unavailable, in table order.",
    )
    placement_error: str | None = Field(
        default=None,
        description="Why the placement state could not be read, when it could not.",
    )
    hosts: list[ModelLabHost] = Field(default_factory=list)
    receipts: list[ModelLabReceipt] = Field(
        default_factory=list, description="Receipts in the order the caller read them."
    )
    live_marker_hosts: list[str] = Field(
        default_factory=list,
        description="One host name per placement marker whose process is alive.",
    )


class ModelThroughputTickRequest(BaseModel):
    """Facts the caller read from the landing controller's files, launchd and the PR watcher.

    The handler reads nothing and has no clock; every field is a fact the caller observed.
    A fact the caller could not read is `None` or an error string, never a guess.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    now: str = Field(pattern=_TS)
    controller_max_minutes: int = Field(default=15, ge=0)
    controller_run_max_minutes: int = Field(default=30, ge=0)
    watcher_max_minutes: int = Field(default=15, ge=0)
    min_merges_per_hour: int = Field(default=5, ge=0)

    launchctl: Literal["loaded", "not_loaded", "unavailable"]
    launchctl_error: str | None = None
    launchctl_stdout: str = ""
    pid_etime: str | None = Field(
        default=None,
        description="Stripped `ps -o etime=` output for the launchd PID, None when ps failed.",
    )
    ticks: list[dict[str, JsonType]] = Field(
        default_factory=list,
        description="The newest complete controller tick lines, newest first, at most two.",
    )
    ticks_error: str | None = Field(
        default=None,
        description="Why ticks.jsonl could not be read, when it could not.",
    )
    heartbeat_text: str | None = None
    heartbeat_write_error: str | None = Field(
        default=None,
        description=(
            "`<path>: <error>` when the caller could not write the tick heartbeat file; "
            "reported as the first finding."
        ),
    )
    state_json: JsonType | None = Field(
        default=None,
        description="The controller's state.json, parsed; None when unreadable.",
    )

    watcher_path: str | None = None
    watcher_state: JsonType | None = None
    watcher_read_error: str | None = None

    floors_per_repo: dict[str, float] | None = None
    floors_error: str | None = None
    policy_load_error: str | None = None
    policy_error: str = ""
    land_like_operator: list[str] = Field(default_factory=list)

    lab_headroom: ModelLabHeadroomFacts | None = Field(
        default=None,
        description="Lab-headroom facts; None skips the finding (and its `checked` entry).",
    )

    pr_land: ModelLabFillPrLandFacts | None = Field(
        default=None,
        description=(
            "Open PRs with their landing facts and hold source (OMN-20864): the lab-headroom FIX names "
            "pr-land lanes for idle slots on parked, escalated or unowned-red PRs, by the rule lab-fill "
            "selection runs; None leaves the FIX as it was."
        ),
    )

    park_max_hours: float = Field(
        default=2.0,
        ge=0,
        description=(
            "A park over a repository under its floor with zero merges in the window covers it as a NOTE "
            "only while it ends within this many hours; a longer park is MISSING (OMN-20840)."
        ),
    )
    dispatch_window_hours: float | None = Field(
        default=None,
        gt=0,
        description=(
            "Receipts started this many hours back that ended without running are findings; None skips "
            "the dispatches finding (and its `checked` entry)."
        ),
    )
    open_history: list[ModelOpenPoint] | None = Field(
        default=None,
        description=(
            "Open counts earlier tick runs recorded, oldest first; None skips the open-trend finding "
            "(and its `checked` entry)."
        ),
    )
    open_rise_pct: float = Field(default=10.0, ge=0)
    open_history_max_age_hours: float = Field(
        default=3.0,
        gt=0,
        description="Points older than this belong to another session and are not a trend.",
    )


class ModelThroughputTickResult(BaseModel):
    """The tick's finding lines and its status line, exactly as the retired script printed them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lines: list[str]
    status_line: str
    missing: list[str]
    unknown: list[str]
    checked: list[str]
    exit_code: int = Field(ge=0, le=1)
    open_count: int | None = Field(
        default=None,
        description="The open PR count read from a fresh watcher state, for the caller to record; None when unread.",
    )
    pr_land: ModelLabFillPrLandPlan | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description=(
            "The idle-slot pr-land fallback the lab-headroom FIX names (OMN-20864); None without PR-land "
            "facts or with the lab's capacity unread, and then absent from the dump, so a caller that "
            "sends no PR-land facts reads the result it read before."
        ),
    )
