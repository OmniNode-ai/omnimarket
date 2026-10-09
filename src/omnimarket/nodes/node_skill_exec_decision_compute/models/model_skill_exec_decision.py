# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request and result for the skill executor's decisions (OMN-20686)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelSkillExecRequest(BaseModel):
    """What the executor knows when it asks for a decision.

    ``plan``: the argument text, the environment values the executor reads and any file facts the
    caller already read; the answer is the command to run, a refusal, or the facts still needed.
    ``report``: the plan's command with what the process printed; the answer is the executor's
    whole output and exit code. ``usage``: the skills whose usage text is wanted.

    The handler reads no file, runs no process and has no clock. A fact the caller could not read
    is ``None``, never a guess.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    phase: Literal["plan", "report", "usage"]
    skill: str = ""
    argument_text: str | None = Field(
        default=None,
        description="The argument file's text; None when it could not be read.",
    )
    read_error: str | None = Field(
        default=None,
        description="Why the argument file could not be read (the OS error text).",
    )
    args_file: str = Field(
        default="",
        description="Path of the argument file; body files are written beside it.",
    )
    plugin_dir: str = Field(
        default="",
        description="The omni plugin directory that holds scripts/ and skills/.",
    )
    python: str = Field(
        default="", description="The interpreter that runs onex_ledger.py."
    )
    env: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "ONEX_DRY_RUN, OMNI_HOME, ONEX_LANE_ACTOR, ONEX_LANE_MODEL, SESSION_LANE and "
            "OMNI_SKILL_EXEC_BUDGET_S as the executor holds them, when set."
        ),
    )
    session_env: dict[str, str] | None = Field(
        default=None, description="The variables a sourced --session-env file sets."
    )
    session_env_error: str | None = Field(
        default=None, description="Why that file could not be used (the refusal text)."
    )
    texts: dict[str, str | None] = Field(
        default_factory=dict,
        description="For each path the plan asked to read, its UTF-8 text; None when unreadable.",
    )
    exists: dict[str, bool] = Field(
        default_factory=dict,
        description="For each path the plan asked about, whether it is a file.",
    )
    command: list[str] = Field(
        default_factory=list, description="report: the command that ran."
    )
    returncode: int = Field(default=0, description="report: the child's exit status.")
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    elapsed_s: float = Field(
        default=0.0, ge=0.0, description="report: seconds since the executor began."
    )
    usage_skills: list[str] = Field(
        default_factory=list,
        description="usage: the skills asked about; empty means all.",
    )


class ModelSkillExecResult(BaseModel):
    """The executor's next step."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["need", "refused", "command", "done", "usage"]
    exit_code: int | None = None
    output: str = Field(default="", description="Printed on stdout, byte for byte.")
    stderr: str = Field(default="", description="Printed on stderr (usage only).")
    command: list[str] = Field(default_factory=list)
    write_files: dict[str, str] = Field(
        default_factory=dict,
        description="Files to write (UTF-8) before the command runs.",
    )
    env_updates: dict[str, str] = Field(
        default_factory=dict,
        description="Variables to set in the command's environment.",
    )
    budget_s: float | None = Field(
        default=None, description="Seconds the command may run."
    )
    need_session_env: str | None = Field(
        default=None,
        description="A session.env path to source; send its variables back.",
    )
    need_texts: list[str] = Field(default_factory=list)
    need_exists: list[str] = Field(default_factory=list)
