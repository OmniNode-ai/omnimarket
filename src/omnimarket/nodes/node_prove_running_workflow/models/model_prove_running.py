# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed inputs and outputs of the prove-running batch and child workflows.

The workflow carries these models as JSON strings so its history holds exactly what the caller sent. Every step is a
plain command (one of our nodes, through ``onex run-node`` or the node's declared CLI); the command reads one
``ModelProveRunningStepInput`` on stdin and answers with one JSON line on stdout.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.integration_reading import ModelIntegrationReadingBatch


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EnumProveRunningOutcome(StrEnum):
    """How one prove-running workflow ended."""

    PROVED = "proved"
    ALARM = "alarm"


class EnumProveRunningResultKind(StrEnum):
    """The one kind of every result row: proven (carries its reading), blocked (carries its owner) or next-step
    (carries the one next step)."""

    PROVEN = "proven"
    BLOCKED = "blocked"
    NEXT_STEP = "next-step"


class EnumProveRunningLineCase(StrEnum):
    """Why the batch, not the change's own workflow, writes a line's result row."""

    ALREADY_STARTED = "already-started"
    UNRESOLVED = "merge-sha-unread"
    CHILD_FAILED = "child-failed"


class ModelProveRunningChange(_Frozen):
    """One merged change and the place it should run (a host or a runtime)."""

    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    number: int = Field(gt=0)
    merge_sha: str = Field(pattern=r"^[0-9a-f]{7,40}$")
    target: str = Field(min_length=1, pattern=r"^\S+$")
    # The integration line's own owner and next step: a result row that is not a proof carries one of them.
    owner: str = ""
    next_step: str = ""

    @property
    def key(self) -> str:
        return f"{self.repository}#{self.number}"

    @property
    def workflow_id(self) -> str:
        """``<repo>#<n>@<sha>``: one workflow per change, so a change is never proven twice."""
        return f"{self.key}@{self.merge_sha}"


class ModelProveRunningSteps(_Frozen):
    """The argument vector of each step; every one names a node command, never inline logic."""

    read_status: tuple[str, ...] = Field(min_length=1)
    install: tuple[str, ...] = Field(min_length=1)
    read_running: tuple[str, ...] = Field(min_length=1)
    write_status: tuple[str, ...] = Field(min_length=1)
    write_alarm: tuple[str, ...] = Field(min_length=1)


class ModelProveRunningPlan(_Frozen):
    """One workflow's input: the change, its steps, the durable wait, the retry limit and the deadline."""

    change: ModelProveRunningChange
    steps: ModelProveRunningSteps
    wait_seconds: int = Field(gt=0)
    max_readings: int = Field(gt=0, le=100)
    deadline_seconds: int = Field(gt=0)
    step_timeout_seconds: int = Field(default=600, gt=0)
    step_max_attempts: int = Field(default=3, gt=0, le=20)
    batch_id: str = ""

    @model_validator(mode="after")
    def _deadline_covers_one_wait(self) -> ModelProveRunningPlan:
        if self.deadline_seconds < self.wait_seconds:
            raise ValueError("the deadline is at least one durable wait long")
        return self


class ModelProveRunningAnswer(_Frozen):
    """What a read step answers: installed or not, running or not, where, at which revision, and what was seen."""

    installed: bool
    running: bool
    host: str = Field(min_length=1)
    revision: str = ""
    observed: str = ""
    detail: str = ""
    read_at: str = Field(min_length=1)


class ModelProveRunningStepInput(_Frozen):
    """What every step command reads on stdin."""

    step: str = Field(min_length=1)
    step_key: str = Field(min_length=1)
    change: ModelProveRunningChange
    last_reading: ModelProveRunningAnswer | None = None
    batch_id: str = ""


class ModelProveRunningResult(_Frozen):
    """How a workflow ended, with the reading it ended on and how many readings it took."""

    workflow_id: str
    outcome: EnumProveRunningOutcome
    readings: int = Field(ge=0)
    installed_by_workflow: bool
    last_reading: ModelProveRunningAnswer | None = None


class ModelProveRunningRowRequest(_Frozen):
    """Build the ledger row for an ended workflow: a STATUS row when proved, an alarm row when not."""

    outcome: EnumProveRunningOutcome
    change: ModelProveRunningChange
    reading: ModelProveRunningAnswer | None
    step_key: str = Field(min_length=1)
    lane: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9-]*$")
    ticket: str = Field(pattern=r"^OMN-\d+$")
    written_at: str = Field(min_length=1)
    batch_id: str = ""


class ModelProveRunningRowResult(_Frozen):
    """The one ledger row the write step appends."""

    row: str = Field(min_length=1)


class ModelProveRunningEarlier(_Frozen):
    """What the change's own workflow, started by an earlier batch, stands at: its status and, once it ended, its result."""

    workflow_id: str
    status: str = Field(min_length=1)
    result: ModelProveRunningResult | None = None


class ModelProveRunningLinePayload(_Frozen):
    """A NOT RUNNING line whose result row the batch writes itself: it was started before, never became a start, or its
    own workflow failed."""

    case: EnumProveRunningLineCase
    key: str = Field(min_length=1)
    target: str = Field(min_length=1)
    owner: str = ""
    next_step: str = ""
    workflow_id: str = ""
    merge_sha: str = ""
    earlier: ModelProveRunningEarlier | None = None
    detail: str = ""


class ModelProveRunningLineWritten(_Frozen):
    """What the line-row write step answers: the row it appended and its kind."""

    written: bool
    row: str
    result: EnumProveRunningResultKind


class ModelProveRunningLineRowRequest(ModelProveRunningLinePayload):
    """The payload with what the row needs from the node's declaration and the caller's clock."""

    step_key: str = Field(min_length=1)
    lane: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9-]*$")
    ticket: str = Field(pattern=r"^OMN-\d+$")
    written_at: str = Field(min_length=1)
    batch_id: str = ""


class ModelProveRunningStartRequest(_Frozen):
    """The starter's input: one integration pass's readings. Every NOT RUNNING line is started; there is no cap."""

    batch: ModelIntegrationReadingBatch


class ModelProveRunningUnresolvedLine(_Frozen):
    """A NOT RUNNING line that could not become a start (its merge sha is unread), with what its result row carries."""

    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    number: int = Field(gt=0)
    target: str = Field(min_length=1)
    owner: str = ""
    next_step: str = ""
    reason: str = Field(min_length=1)

    @property
    def key(self) -> str:
        return f"{self.repository}#{self.number}"


class ModelProveRunningStartBatch(_Frozen):
    """One start per NOT RUNNING line, oldest first, the lines that could not be started with the reason, and how many
    lines were read: ``lines_read`` equals ``len(changes) + len(unresolved)``."""

    changes: tuple[ModelProveRunningChange, ...]
    skipped: tuple[str, ...] = ()
    unresolved: tuple[ModelProveRunningUnresolvedLine, ...] = ()
    lines_read: int = Field(default=0, ge=0)


class ModelProveRunningBatchSteps(_Frozen):
    """The node commands that select changes and prepare child workflow plans."""

    read_not_running: tuple[str, ...] = Field(min_length=1)
    group_by_host: tuple[str, ...] = Field(min_length=1)
    write_line_result: tuple[str, ...] = Field(min_length=1)


class ModelProveRunningBatchPlan(_Frozen):
    """The scheduled workflow input and its activity limits. A schedule created before the cap was removed still
    carries ``limit``; it is dropped on read, so an old schedule input never caps the lines read."""

    steps: ModelProveRunningBatchSteps
    step_timeout_seconds: int = Field(gt=0)
    step_max_attempts: int = Field(gt=0, le=20)

    @model_validator(mode="before")
    @classmethod
    def _drop_legacy_limit(cls, data: object) -> object:
        if isinstance(data, dict) and "limit" in data:
            return {k: v for k, v in data.items() if k != "limit"}
        return data


class ModelProveRunningBatchStepInput(_Frozen):
    """One batch command's input, including the previous step's JSON answer."""

    step: str = Field(min_length=1)
    step_key: str = Field(min_length=1)
    batch_id: str = Field(min_length=1)
    payload: str = ""


class ModelProveRunningHostGroup(_Frozen):
    """The child workflow plans for a host."""

    host: str
    plans: tuple[ModelProveRunningPlan, ...]


class ModelProveRunningHostGroups(_Frozen):
    """Grouped child plans and reasons any ledger changes were skipped."""

    groups: tuple[ModelProveRunningHostGroup, ...]
    skipped: tuple[str, ...] = ()


class ModelProveRunningBatchResult(_Frozen):
    """Counts and outcomes from one scheduled workflow run: ``lines_read`` NOT RUNNING lines read, ``results_written``
    result rows written (one per line), ``results`` the kind of each (proven, blocked or next-step)."""

    batch_id: str
    read_changes: int
    hosts: dict[str, int]
    started: tuple[str, ...]
    already_proven_or_running: tuple[str, ...]
    outcomes: dict[str, str]
    lines_read: int = 0
    results_written: int = 0
    results: dict[str, str] = Field(default_factory=dict)


class ModelProveRunningHostsOverlay(_Frozen):
    """The deployment overlay of the batch workflow: the host names a change's target is matched against, in order."""

    hosts: tuple[str, ...] = Field(min_length=1)
