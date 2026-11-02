# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed input and result of the launch validity check.

The check answers one question at the moment a lane is launched: is the lane's target
still worth a slot. The caller reads the live state (the PR watcher, one PR read per named
PR, the ledger's live CLAIMs) and passes each read in ``reads``; a read that failed or
could not be made is itself a fact (``ok`` false), and the check refuses on it, naming it.
Every limit and the mode are in ``ModelLaunchValidityPolicy``, which the caller builds
from the node contract's ``config.launch_validity`` and the ``launch_validity`` overlay.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumLaunchValidityMode(StrEnum):
    """What a refusal does: ``report_only`` records it and never blocks, ``refuse`` blocks the launch."""

    REPORT_ONLY = "report_only"
    REFUSE = "refuse"


class EnumLaunchValidityTransport(StrEnum):
    """The bus binding the decision travels over; the decision itself is the same on both."""

    IN_MEMORY = "in_memory"
    REDPANDA = "redpanda"


class EnumRead(StrEnum):
    """One live read the check needs."""

    PR = "pr"
    TICKET = "ticket"
    PLAN_TASK = "plan_task"
    OWNER = "owner"


class EnumRefusalCode(StrEnum):
    """Why a launch is refused."""

    PR_NOT_OPEN = "pr-not-open"
    PR_QUEUED = "pr-queued"
    PR_MERGED = "pr-merged"
    PR_CLOSED = "pr-closed"
    PR_HEAD_MISMATCH = "pr-head-mismatch"
    TICKET_DONE = "ticket-done"
    PLAN_TASK_DONE = "plan-task-done"
    LIVE_OWNER = "live-owner"
    READ_FAILED = "read-failed"
    READ_MISSING = "read-missing"
    READ_STALE = "read-stale"


class ModelStateRead(BaseModel):
    """The result of one live read, as the caller made it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    detail: str = ""
    age_seconds: int | None = None
    state: str | None = None
    head: str | None = None
    pr_class: str | None = None
    done_by: tuple[str, ...] = ()
    owners: tuple[str, ...] = ()


class ModelReadRequirement(BaseModel):
    """A read the check needs whenever the launch names ``target_field``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    read: EnumRead
    target_field: str


class ModelLaunchValidityPolicy(BaseModel):
    """Mode and limits, from the contract's ``config.launch_validity`` and the overlay."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: EnumLaunchValidityMode
    transport: EnumLaunchValidityTransport
    max_read_age_seconds: int = Field(ge=0)
    min_head_chars: int = Field(ge=1)
    pr_open_state: str
    pr_state_codes: dict[str, EnumRefusalCode]
    pr_class_codes: dict[str, EnumRefusalCode]
    done_states: tuple[str, ...]
    reads: tuple[ModelReadRequirement, ...]


class ModelLaunchTarget(BaseModel):
    """What the launch names. A field left ``None`` is a target the launch does not name."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str
    ticket: str | None = None
    plan_task: str | None = None
    pr: str | None = None
    expected_head: str | None = None


class ModelLaunchValidityRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    target: ModelLaunchTarget
    reads: dict[EnumRead, ModelStateRead] = Field(default_factory=dict)
    policy: ModelLaunchValidityPolicy


class ModelRefusal(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    code: EnumRefusalCode
    read: EnumRead
    detail: str


class ModelWouldRefuseRecord(BaseModel):
    """What report-only mode emits for a launch it would have refused: one record per refused launch."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str
    ticket: str | None
    pr: str | None
    refusals: tuple[ModelRefusal, ...]


class ModelLaunchValidityResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str
    mode: EnumLaunchValidityMode
    refusals: tuple[ModelRefusal, ...]
    blocks: bool
    would_refuse: ModelWouldRefuseRecord | None


class ModelRecordedLaunch(BaseModel):
    """One recorded launch for the replay: the launch time and the reads as they stood then."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    at: str
    target: ModelLaunchTarget
    reads: dict[EnumRead, ModelStateRead] = Field(default_factory=dict)


class ModelLaunchValidityReplayRequest(BaseModel):
    """Recorded launches to replay under one policy."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    launches: tuple[ModelRecordedLaunch, ...]
    policy: ModelLaunchValidityPolicy


class ModelReplayResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    launches: int
    would_refuse: int
    by_code: dict[EnumRefusalCode, int]
    lanes: tuple[str, ...]


class ModelLaunchValidityTopics(BaseModel):
    """The node's topics, read from its contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command: str
    decided: str
    would_refuse: str
