# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Inputs to report publication, independent of the scanning process."""

from datetime import date
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ModelReportRoot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str = Field(pattern=r"^[\w.-]+$")
    registry: Path
    worktrees: Path


class ModelWorktreeReportPublishRequest(BaseModel):
    """Publish only the classifier's completed reports; no removal authority."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    date: date
    repository: str = Field(pattern=r"^[\w.-]+/[\w.-]+$")
    ticket: str = Field(pattern=r"^OMN-[0-9]+$")
    lane: str = Field(pattern=r"^[\w.-]+$")
    checkout: Path
    worktrees_root: Path
    state_root: Path
    ledger: Path
    names_file: Path
    alert_script: Path
    reports: tuple[Path, ...] = Field(min_length=1)
    roots: tuple[ModelReportRoot, ...] = Field(min_length=1)
    home: Path
    executed: bool
    summary: str
    lab: str = Field(pattern=r"^Lab: [^\n]+$")

    @field_validator("reports")
    @classmethod
    def report_paths(cls, reports: tuple[Path, ...]) -> tuple[Path, ...]:
        if any(
            not p.is_absolute() or p.suffix not in {".md", ".json"} for p in reports
        ):
            raise ValueError("reports must be absolute markdown or JSON paths")
        if len({p.name for p in reports}) != len(reports):
            raise ValueError("report basenames must be unique")
        return reports

    @model_validator(mode="after")
    def absolute_paths(self) -> "ModelWorktreeReportPublishRequest":
        paths = [
            self.checkout,
            self.worktrees_root,
            self.state_root,
            self.ledger,
            self.names_file,
            self.alert_script,
            self.home,
            *(root.registry for root in self.roots),
            *(root.worktrees for root in self.roots),
        ]
        if any(not path.is_absolute() for path in paths):
            raise ValueError("host paths must be absolute")
        return self


class ModelWorktreeReportPublishResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    outcome: Literal["handed-off", "failed"]
    pr_number: int = 0
    head: str = ""
    step: str = ""
    detail: str = ""
    friction_recorded: bool = False
    page_delivered: bool = False
