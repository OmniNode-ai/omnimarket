# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request and result for the onex-ledger wrapper's admission decisions (OMN-20686)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelOnexLedgerAdmissionRequest(BaseModel):
    """What the caller read before it would run ``onex-ledger``: the argv, two environment values,
    the clone candidates it resolved, each candidate's project name and the append-rows file.

    The handler reads nothing, runs nothing and has no clock. A fact the caller could not read is
    ``None``, never a guess.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    argv: list[str]
    budget_raw: str = Field(
        default="", description="ONEX_LEDGER_BUDGET_S as set, empty when unset."
    )
    path_override: str = Field(
        default="",
        description="OMNIBASE_INTERNAL_PATH as set, stripped; empty when unset.",
    )
    omni_home: str = Field(
        default="", description="OMNI_HOME as set, stripped; empty when unset."
    )
    override_resolved: str | None = Field(
        default=None,
        description="The expanded, resolved path of path_override; None when there is no override.",
    )
    default_resolved: str | None = Field(
        default=None,
        description="The resolved parent of omni_home joined with omnibase_internal; None when omni_home is unset.",
    )
    project_names: dict[str, str | None] = Field(
        default_factory=dict,
        description=(
            "For each candidate path text, the [project] name in its pyproject.toml; None when it is not a "
            "directory with a readable pyproject.toml that has a project table."
        ),
    )
    append_rows_text: str | None = Field(
        default=None,
        description="The contents of the file named by the last argv item for append-rows; None when unreadable.",
    )


class ModelOnexLedgerAdmissionResult(BaseModel):
    """What the wrapper does: run onex-ledger in `project`, or print the lines and exit."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action: Literal["run", "refuse", "usage-error", "no-project"]
    exit_code: int | None = Field(
        default=None, description="The wrapper's exit code; None for `run`."
    )
    stdout_lines: list[str] = Field(default_factory=list)
    stderr_lines: list[str] = Field(default_factory=list)
    project: str | None = None
    budget_s: float | None = None
    timeout_stdout: str | None = Field(
        default=None,
        description="Printed on stdout when the budget runs out; the exit code is then 124.",
    )
    timeout_stderr: str | None = Field(
        default=None,
        description="Printed on stderr when the budget runs out; the exit code is then 124.",
    )
