# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request, action, result and seam models of the delegated code edit loop (OMN-20290).

Canonical home of these models, promoted out of
``node_delegated_code_edit_orchestrator`` so other nodes consume them without
a cross-node model reach-in (OMN-21373).

One loop edits one worktree toward one task. Each model turn is one
``onex delegate`` run whose reply is a typed list of actions; the loop applies
them through its ports, confined to the worktree and to the paths the request
declares writable, and runs only the checks the request declares. The tool
names match the ones the crush agent offers (``view``, ``ls``, ``grep``,
``write``, ``edit``, ``replace_in_files``) so the tool_use rubric scores both
engines on one vocabulary.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from omnibase_core.enums.enum_delegation_traffic_class import (
    EnumDelegationTrafficClass,
)
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

#: The tool_use rubric's turn budget (delegation_class_rubrics.v1.yaml).
MAX_TURNS_CEILING = 40
#: Actions one turn may carry.
MAX_ACTIONS_PER_TURN = 12
#: Files one bulk replacement may target.
MAX_BULK_FILES = 200
#: Bytes of one file the loop shows the model.
MAX_VIEW_BYTES = 60_000
#: Bytes of one file the model may write.
MAX_WRITE_BYTES = 200_000
#: Bytes of tool output fed back per action.
MAX_OBSERVATION_BYTES = 6_000
#: Characters of one error retained in the loop receipt.
MAX_ERROR_CHARS = 4096
#: Lines one view shows; a longer file is paged with ``offset``.
VIEW_WINDOW_LINES = 250
#: Bytes one view window may carry.
MAX_VIEW_WINDOW_BYTES = 16_000
#: Characters the reads (view, grep, ls) of one turn may show together. The
#: history holds about 66,000 characters, so a turn's reads fit it whole with
#: room for the turn before (OMN-20291).
MAX_READ_CHARS_PER_TURN = 30_000
#: Below this many characters left, a turn's further reads are not run.
MIN_READ_CHARS = 2_000
#: Turns in a row that read and change no file before the next turn's reads are
#: refused, so a model that cannot hold every file it wants to read writes with
#: what it has instead of reading to the turn cap (OMN-20291).
MAX_READ_ONLY_TURNS = 3

_CHECK_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")


def bound_error(text: str, limit: int = MAX_ERROR_CHARS) -> str:
    """Bound an error while preserving its head, tail and exact cut count."""
    if len(text) <= limit:
        return text
    if limit < 64:
        return text[:limit]
    cut = len(text) - limit
    while True:
        marker = f"\n... [{cut} characters cut] ...\n"
        share = limit - len(marker)
        removed = len(text) - share
        if removed == cut:
            break
        cut = removed
    head = (share + 1) // 2
    tail = share // 2
    return text[:head] + marker + text[-tail:]


class EnumCodeEditTool(StrEnum):
    VIEW = "view"
    LS = "ls"
    GREP = "grep"
    WRITE = "write"
    EDIT = "edit"
    REPLACE_IN_FILES = "replace_in_files"
    FORMAT = "format"
    RUN_CHECK = "run_check"
    FINISH = "finish"


#: Tools that change the worktree.
WRITING_TOOLS = frozenset(
    {
        EnumCodeEditTool.WRITE,
        EnumCodeEditTool.EDIT,
        EnumCodeEditTool.REPLACE_IN_FILES,
        EnumCodeEditTool.FORMAT,
    }
)


class EnumCodeEditStatus(StrEnum):
    ACCEPTED = "accepted"
    CHECKS_FAILED = "checks_failed"
    NO_PROGRESS = "no_progress"
    NO_CHANGE = "no_change"
    BUDGET_EXHAUSTED = "budget_exhausted"
    DELEGATE_FAILED = "delegate_failed"
    INFRA_ERROR = "infra_error"


class ModelDeclaredCheck(BaseModel):
    """One check the model may ask for by name. Never a shell string."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    argv: tuple[str, ...] = Field(..., min_length=1)
    timeout_seconds: int = Field(default=300, ge=1, le=1800)
    targets: tuple[str, ...] = Field(
        default=(),
        description="Test targets this check runs (tests/...), so the tool_use "
        "rubric can bind a passing check to the answer that names it.",
    )

    @field_validator("name", mode="before")
    @classmethod
    def _name(cls, value: str) -> str:
        """Normalise a lane-brief name (``PYTEST``) before the rule is applied."""
        if isinstance(value, str):
            value = re.sub(r"[^a-z0-9_-]+", "-", value.strip().lower()).strip("-")
        if not _CHECK_NAME.fullmatch(value):
            raise ValueError(
                "a check name is lowercase letters, digits, '_' or '-', at most 48"
            )
        return value


class ModelDelegatedCodeEditRequest(BaseModel):
    """One task, one worktree, and the rails the loop keeps."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    task: str = Field(..., min_length=1, max_length=20_000)
    workspace_root: str = Field(
        ..., min_length=1, description="Absolute path of the git worktree."
    )
    writable_globs: tuple[str, ...] = Field(
        ...,
        min_length=1,
        description="Worktree-relative globs the model may write: * and ? stay "
        "inside one path segment, ** crosses segments.",
    )
    context_paths: tuple[str, ...] = Field(
        default=(), description="Worktree-relative files shown in the first turn."
    )
    file_list: tuple[str, ...] = Field(
        default=(),
        description="Worktree-relative files the task names. When given, "
        "replace_in_files reaches only these (a glob narrows the list, never "
        "widens it); the writable globs still apply. Empty: no list.",
    )
    checks: tuple[ModelDeclaredCheck, ...] = Field(..., min_length=1)
    formatter: tuple[Annotated[tuple[str, ...], Field(min_length=1)], ...] = Field(
        default=(),
        description="Ordered formatter argvs the format tool runs; the file's "
        "worktree-relative path is appended to each. Stop at the first failed "
        "command. Empty: no format tool.",
    )
    max_turns: int = Field(default=20, ge=1, le=MAX_TURNS_CEILING)
    wall_budget_seconds: int | None = Field(
        default=None,
        ge=1,
        le=7200,
        description="Wall seconds the loop may use; None means no limit. The loop starts no "
        "turn that its slowest turn so far would not finish inside this budget.",
    )
    max_no_change_finishes: int = Field(
        default=2,
        ge=1,
        le=10,
        description="How many finish calls over an empty diff end the loop as no_change.",
    )
    task_type: Literal["code_generation"] = "code_generation"
    caller_lane: str | None = Field(
        default=None,
        description="The ledger lane each onex delegate run is attributed to.",
    )
    ticket: str | None = Field(default=None, pattern=r"^OMN-[0-9]+$")
    traffic_class: EnumDelegationTrafficClass = Field(
        default=EnumDelegationTrafficClass.UNCLASSIFIED,
        description="Provenance of every delegate turn: put on each turn's argv "
        "as --traffic-class (core ModelDelegationProvenance). Synthetic marks an "
        "eval or probe run as not a user's run and requires source_surface. A "
        "caller that names no class reads as unclassified, never as organic.",
    )
    source_surface: str | None = Field(
        default=None,
        min_length=1,
        description="Named producer surface put on each turn's argv as "
        "--source-surface.",
    )

    @field_validator("correlation_id")
    @classmethod
    def _uuid(cls, value: str) -> str:
        UUID(value)
        return value

    @field_validator("workspace_root")
    @classmethod
    def _absolute(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("workspace_root must be an absolute path")
        return value.rstrip("/") or "/"

    @field_validator("writable_globs", "context_paths", "file_list")
    @classmethod
    def _relative(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for item in value:
            if not item or item.startswith("/") or ".." in item.split("/"):
                raise ValueError(f"{item!r} is not a worktree-relative path")
        return value

    @model_validator(mode="after")
    def _synthetic_names_its_surface(self) -> ModelDelegatedCodeEditRequest:
        if (
            self.traffic_class is EnumDelegationTrafficClass.SYNTHETIC
            and not self.source_surface
        ):
            raise ValueError("synthetic traffic requires a source_surface")
        return self

    @model_validator(mode="after")
    def _unique_checks(self) -> ModelDelegatedCodeEditRequest:
        names = [check.name for check in self.checks]
        if len(names) != len(set(names)):
            raise ValueError("duplicate check names")
        return self

    def check_named(self, name: str) -> ModelDeclaredCheck | None:
        return next((c for c in self.checks if c.name == name), None)


class ModelCodeEditAction(BaseModel):
    """One action of one turn, as the reply parser validated it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool: EnumCodeEditTool
    path: str = ""
    offset: int = Field(
        default=0, ge=0, description="view: first line to show (1-based)."
    )
    file_path: str = ""
    file_paths: tuple[str, ...] = ()
    glob: str = ""
    pattern: str = ""
    content: str = ""
    old_string: str = ""
    new_string: str = ""
    name: str = ""
    summary: str = ""

    @property
    def target(self) -> str:
        """The worktree-relative path (or, for a bulk edit, the glob) the action names, if any."""
        return self.file_path or self.path or self.glob


class ModelDelegatePin(BaseModel):
    """The routing backend every model turn is pinned to (OMN-20514).

    Resolved from the routing contract before the first turn, so the host a
    turn will use is known, printed and recorded ahead of the first model call.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    backend_id: str
    host: str
    endpoint_url: str
    model_name: str = ""


class ModelTurnReply(BaseModel):
    """What one ``onex delegate`` turn returned."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    ok: bool
    actions: tuple[ModelCodeEditAction, ...] = ()
    invalid_reason: str = ""
    raw_text: str = Field(default="", max_length=200_000)
    tokens_in: int = 0
    tokens_out: int = 0
    model: str = ""
    #: The model revision the endpoint reported for this turn's response; empty when it reported none (OMN-24173).
    model_revision: str = ""


class ModelObservation(BaseModel):
    """The environment's answer to one action."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    output: str = Field(default="", max_length=MAX_VIEW_WINDOW_BYTES + 400)
    refused: bool = Field(
        default=False,
        description="The loop did not run the action (a write outside the "
        "writable scope, a read while reads are paused): it changed and showed "
        "nothing, so the tool_use budget counts it apart from work.",
    )


class ModelCheckResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    status: Literal["passed", "failed", "timeout", "infra_error"]
    exit_code: int | None = None
    output_tail: str = Field(default="", max_length=MAX_OBSERVATION_BYTES + 200)
    fingerprint: str = ""
    duration_ms: int = 0


class ModelCodeEditResult(BaseModel):
    """The compact result the caller reads. No file content, no logs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    loop_run_id: str
    status: EnumCodeEditStatus
    turns: int = Field(..., ge=0, le=MAX_TURNS_CEILING)
    delegate_run_ids: tuple[str, ...] = ()
    changed_paths: tuple[str, ...] = ()
    diff_sha256: str = ""
    resumable: bool = False
    checks: tuple[ModelCheckResult, ...] = ()
    refusals: int = Field(default=0, ge=0)
    rubric_outcome: str = ""
    local_tokens_in: int = 0
    local_tokens_out: int = 0
    wall_ms: int = 0
    #: The one model revision every answering turn reported; empty when a turn reported none or two differed (OMN-24173).
    model_revision: str = ""
    summary: str = Field(default="", max_length=1000)
    detail: str = Field(default="", max_length=300)
    terminal_failure_cause: str | None = None


__all__ = [
    "MAX_ACTIONS_PER_TURN",
    "MAX_BULK_FILES",
    "MAX_ERROR_CHARS",
    "MAX_OBSERVATION_BYTES",
    "MAX_TURNS_CEILING",
    "MAX_VIEW_BYTES",
    "MAX_VIEW_WINDOW_BYTES",
    "MAX_WRITE_BYTES",
    "VIEW_WINDOW_LINES",
    "WRITING_TOOLS",
    "EnumCodeEditStatus",
    "EnumCodeEditTool",
    "ModelCheckResult",
    "ModelCodeEditAction",
    "ModelCodeEditResult",
    "ModelDeclaredCheck",
    "ModelDelegatePin",
    "ModelDelegatedCodeEditRequest",
    "ModelObservation",
    "ModelTurnReply",
    "bound_error",
]
