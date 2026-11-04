# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Caller-supplied facts for the one-command skill entrypoint helpers and the lines they decide."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

EntrypointLibOperation = Literal[
    "refuse",
    "dry_run_guard",
    "require_env",
    "detail_dir",
    "verb_budget",
    "start_line",
    "step_start_line",
    "step_end_line",
    "timing_line",
    "too_old",
    "verb_failed",
    "last",
    "field",
]


class ModelEntrypointStepTiming(BaseModel):
    """One finished step of an entrypoint: its name and seconds, already formatted by the caller."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    seconds: str


class ModelEntrypointLibRequest(BaseModel):
    """One question about an entrypoint helper; the caller read every fact it names.

    The caller owns the clock, the environment, the filesystem and the process table. This request
    carries what it read; the result carries the lines to print and the exit code to use.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: EntrypointLibOperation
    code: int | None = None
    rule: str | None = None
    reason: str | None = None
    fix: str | None = None
    timing_line: str | None = Field(
        default=None,
        description="The TIMING line to print on stderr when the entrypoint was started; None when it was not.",
    )
    onex_dry_run: str | None = None
    registry_root: str | None = None
    ledger_path: str | None = None
    sandbox_marker_exists: bool = False
    detail_dir_override: str | None = None
    verb: str | None = None
    budget_override: str | None = None
    entry_name: str | None = None
    detail_path: str | None = None
    step: str | None = None
    budget_seconds: str | None = None
    rc: int | None = None
    took_seconds: str | None = None
    total_seconds: str | None = None
    steps: list[ModelEntrypointStepTiming] = Field(default_factory=list)
    detail_text: str | None = None
    verb_error_suffix: str = ""
    prefix: str | None = None
    line: str | None = None
    key: str | None = None


class ModelEntrypointLibResult(BaseModel):
    """What the shell helper would print and how it would end.

    stdout and stderr are lines without their newline. exit_code is None when the helper returns
    to its caller and an integer when it would exit the entrypoint. value is the single answer of
    a lookup (budget, detail directory, last line, field value), None when there is none.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: EntrypointLibOperation
    stdout: list[str] = Field(default_factory=list)
    stderr: list[str] = Field(default_factory=list)
    exit_code: int | None = None
    value: str | None = None
