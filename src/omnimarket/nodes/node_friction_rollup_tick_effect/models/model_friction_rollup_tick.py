# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request, result and settings of the friction rollup tick (OMN-22987)."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ModelFrictionRollupTickRequest(BaseModel):
    """What a launchd plist passes as a small JSON input: where the registry and the ledger are."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    registry_root: str = Field(
        min_length=1, description="Root that holds the default state directory."
    )
    ledger_path: str = Field(
        min_length=1, description="The rolling work ledger to read and append to."
    )
    now: str | None = Field(
        default=None, description="UTC timestamp used instead of the clock."
    )
    dry_run: bool = False
    state_dir: str | None = Field(
        default=None,
        description="Where reports are written; default under registry_root.",
    )
    archive_dir: str | None = Field(
        default=None,
        description="Where the *-split.md files are; default beside the ledger.",
    )


class ModelTickWindow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    hours: int = Field(ge=1)


class ModelFrictionRollupTickSettings(BaseModel):
    """Every setting of the tick; the overlay file holds them all."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_name: str = Field(alias="schema", min_length=1)
    ticket: str = Field(min_length=1)
    umbrella_ticket: str = Field(min_length=1)
    lane: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    model: str = Field(min_length=1)
    windows: tuple[ModelTickWindow, ...]
    top_n: int = Field(ge=1)
    keep_days: int = Field(ge=1)
    max_row_chars: int = Field(ge=4)
    bucket_hours: int = Field(ge=1)
    state_subdir: str = Field(min_length=1)
    latest_stem: str = Field(min_length=1)
    archive_subdir: str = Field(min_length=1)
    archive_glob: str = Field(min_length=1)
    top_budget_chars: int = Field(ge=1)
    new_classes_budget_chars: int = Field(ge=1)
    needs_ticket_budget_chars: int = Field(ge=1)
    appender_command: tuple[str, ...] = Field(min_length=1)
    appender_timeout_seconds: int = Field(ge=1)
    class_rules_path: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_windows(self) -> Self:
        if len(self.windows) < 2:
            raise ValueError("windows needs the primary window and the short window")
        names = [window.name for window in self.windows]
        if len(set(names)) != len(names):
            raise ValueError("a window is declared twice")
        return self

    @property
    def primary(self) -> ModelTickWindow:
        return self.windows[0]

    @property
    def short(self) -> ModelTickWindow:
        return self.windows[1]


class ModelFrictionRollupTickResult(BaseModel):
    """The outcome of one tick; ``exit_code`` is the timer's: 0 done, 1 append refused, 2 usage or unreadable, 3 done with
    class rules unapplied. ``status`` is what the runtime reads to classify the run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["success", "partial", "failure"]
    exit_code: int = Field(ge=0, le=3)
    state: Literal["OK", "DRY-RUN", "APPEND-FAILED", "REFUSED"]
    status_row: str = ""
    log_line: str = ""
    error_message: str = ""
    class_rules: str = ""
    report_path: str = ""
    report_json_text: str = ""
    report_markdown_text: str = ""
    written: tuple[str, ...] = ()
    pruned: tuple[str, ...] = ()
