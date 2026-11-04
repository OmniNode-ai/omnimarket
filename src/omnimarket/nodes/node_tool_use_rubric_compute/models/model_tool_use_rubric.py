# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Caller-supplied facts for the tool-use rubric scoring wrapper and the plan or summary it decides."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

ToolUseRubricOperation = Literal[
    "disabled",
    "scorer_candidates",
    "scorer_python",
    "scorer_command",
    "manifest",
    "summarize",
    "summary_line",
    "claude_version",
    "schema_cache_target",
    "pick_captured_tools",
]


class ModelToolUseManifestEntry(BaseModel):
    """What the caller read for one listed path: whether it is a file and its line count."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    is_file: bool
    line_count: int | None = Field(
        default=None, description="None when the file could not be read."
    )


class ModelToolUseRubricRequest(BaseModel):
    """One question about the scoring wrapper; the caller read every fact it names."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: ToolUseRubricOperation
    rubric_env: str | None = Field(
        default=None, description="Value of ONEX_TOOL_USE_RUBRIC, None when unset."
    )
    scorer_python_env: str | None = None
    lab_work_python_env: str | None = None
    dispatch_venv: str | None = None
    registry_root: str | None = None
    executable_paths: list[str] = Field(
        default_factory=list,
        description="The candidate paths the caller found executable.",
    )
    source: str | None = None
    args: list[str] = Field(default_factory=list)
    run_ref: str | None = None
    out_path: str | None = None
    environ: dict[str, str] = Field(default_factory=dict)
    ls_returncode: int | None = None
    ls_stdout: str = ""
    entries: list[ModelToolUseManifestEntry] = Field(default_factory=list)
    scorer_python: str | None = None
    run_error: str | None = Field(
        default=None,
        description="Text of the OS or timeout error that ended the scorer run.",
    )
    returncode: int | None = None
    stderr: str = ""
    record: dict[str, JsonValue] | None = Field(
        default=None,
        description="The verdict file the scorer wrote, parsed; None when unreadable.",
    )
    prefix: str | None = None
    summary: dict[str, JsonValue] | None = None
    version_stdout: str = ""
    version: str | None = None
    flags: list[str] = Field(default_factory=list)
    xdg_cache_home: str | None = None
    home: str | None = None
    cache_dir: str | None = None
    captured_bodies: list[dict[str, JsonValue]] = Field(default_factory=list)


class ModelToolUseRubricResult(BaseModel):
    """The answer. Only the fields of the asked operation are set."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: ToolUseRubricOperation
    flag: bool | None = None
    text: str | None = None
    command: list[str] = Field(default_factory=list)
    paths: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    rows: list[dict[str, JsonValue]] = Field(default_factory=list)
    summary: dict[str, JsonValue] | None = None
    verdict_file: dict[str, JsonValue] | None = Field(
        default=None,
        description="What the caller writes to the verdict path; None when the scorer already wrote it.",
    )
    tools: list[JsonValue] | None = None
