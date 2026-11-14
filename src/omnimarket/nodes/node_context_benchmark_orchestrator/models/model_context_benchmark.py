# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models for the paired CLAUDE.md / AGENTS.md on-off benchmark (OMN-21896)."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

#: An absent token count is this string, never 0.
UNKNOWN: Literal["UNKNOWN"] = "UNKNOWN"
TokenCount = int | Literal["UNKNOWN"]

#: The files the "off" arm removes from the copy.
CONTEXT_FILE_NAMES: tuple[str, ...] = ("CLAUDE.md", "AGENTS.md")


class EnumBenchmarkArm(StrEnum):
    ON = "on"
    OFF = "off"


class EnumBenchmarkLane(StrEnum):
    CLAUDE_HEADLESS = "claude_headless"
    CODEX_HEADLESS = "codex_headless"
    DELEGATE_ROUTE = "delegate_route"
    JEV_TYPED_DECISION = "jev_typed_decision"


class EnumRunOutcome(StrEnum):
    """The task's own check result, or why there is none."""

    PASS = "pass"
    FAIL = "fail"
    #: the lane could not be asked this task (Jev given an agent task)
    NOT_APPLICABLE = "not_applicable"
    #: the lane could not run at all (credential absent, no capacity)
    BLOCKED = "blocked"
    #: the lane produced no check result (crash, timeout, parse failure)
    ERROR = "error"


class ModelJevQuestion(BaseModel):
    """A typed decision with a fixed answer set; the only task shape Jev can answer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    instructions: str
    options: dict[str, str] = Field(
        ..., description="option name -> what it covers (every option is described)"
    )
    expected_answer: str


class ModelBenchmarkTask(BaseModel):
    """One task with a machine check."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str
    prompt: str
    check_argv: tuple[str, ...] = Field(
        ..., min_length=1, description="argv run in the arm's copy; exit 0 is a pass"
    )
    edit_files: tuple[str, ...] = Field(
        default=(),
        description="files shown to, and returned by, a text-only delegation route model",
    )
    protected_paths: tuple[str, ...] = Field(
        default=(),
        description="restored from the source before the check, so a run cannot edit its own check",
    )
    jev_question: ModelJevQuestion | None = None


class ModelBenchmarkLaneSpec(BaseModel):
    """One model arm: a lane plus the model it reaches."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: EnumBenchmarkLane
    model_id: str = Field(..., description="the label the report rows carry")
    backend_id: str | None = Field(
        default=None, description="delegation backend pin; none = the lab rung"
    )


class ModelContextBenchmarkRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source_dir: str = Field(..., description="the tree every arm is copied from")
    tasks: tuple[ModelBenchmarkTask, ...]
    lanes: tuple[ModelBenchmarkLaneSpec, ...]
    scratch_dir: str
    min_paired_tasks: int = Field(default=10, ge=1)
    agent_timeout_seconds: float = Field(default=900.0, gt=0)
    check_timeout_seconds: float = Field(default=300.0, gt=0)
    host: str = Field(default="unknown", description="named in a blocked report")
    max_parallel_lanes: int = Field(default=4, ge=1)
    context_file_names: tuple[str, ...] = CONTEXT_FILE_NAMES


class ModelLaneInvocation(BaseModel):
    """What one lane call returned, before the task's check runs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    output_text: str = ""
    input_tokens: TokenCount = UNKNOWN
    output_tokens: TokenCount = UNKNOWN
    cache_read_tokens: TokenCount = UNKNOWN
    cache_write_tokens: TokenCount = UNKNOWN
    served_model: str | None = None
    error: str | None = None
    blocked: str | None = None
    not_applicable: str | None = None
    #: a typed-decision lane answers the check itself
    check_passed: bool | None = None


class ModelRunRecord(BaseModel):
    """One run of one task in one arm; never carries a credential value."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str
    lane: EnumBenchmarkLane
    model_id: str
    arm: EnumBenchmarkArm
    source_sha: str
    outcome: EnumRunOutcome
    input_tokens: TokenCount = UNKNOWN
    output_tokens: TokenCount = UNKNOWN
    cache_read_tokens: TokenCount = UNKNOWN
    cache_write_tokens: TokenCount = UNKNOWN
    served_model: str | None = None
    duration_seconds: float = 0.0
    detail: str = ""


class EnumVerdict(StrEnum):
    CONTEXT_HELPS = "context_helps"
    CONTEXT_HURTS = "context_hurts"
    NO_DETECTABLE_DIFFERENCE = "no_detectable_difference"


class ModelReportRow(BaseModel):
    """One row per model and condition."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_id: str
    lane: EnumBenchmarkLane
    condition: EnumBenchmarkArm
    tasks_run: int
    passes: int
    not_applicable: int
    blocked: int
    errors: int
    mean_tokens: float | Literal["UNKNOWN"]
    mean_fresh_tokens: float | Literal["UNKNOWN"]
    paired_tasks: int
    paired_pass_rate_diff: float | None = Field(
        description="on minus off over tasks with a pass or fail in both arms"
    )
    paired_mean_tokens_diff: float | Literal["UNKNOWN"] | None = None
    on_only_passes: int = 0
    off_only_passes: int = 0
    sign_test_p: float | None = None
    underpowered: bool
    verdict: EnumVerdict | None = Field(
        default=None, description="absent when underpowered"
    )


class ModelContextBenchmarkResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source_sha: str
    host: str
    runs: tuple[ModelRunRecord, ...]
    rows: tuple[ModelReportRow, ...]
    blocked_lanes: tuple[str, ...] = ()
    report_markdown: str = ""
