# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What the dirty-converge tick declares and what it did to one clone (OMN-21821).

A clone that holds local work (a staged or modified tracked file, an untracked file, or a commit no
remote has) is never converged: the tick refuses it, names the kind of work, raises one owned alarm
and leaves the clone as it found it (OMN-19998).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EnumDirtyConvergeStatus(StrEnum):
    CONVERGED = "converged"
    SKIPPED = "skipped"
    FAILED = "failed"
    #: The clone holds local work: nothing moved and an alarm was raised for its owner.
    REFUSED = "refused"


class EnumLocalWorkKind(StrEnum):
    """The kinds of local work a clone can hold that a converge would discard."""

    STAGED = "staged"
    MODIFIED = "modified"
    UNTRACKED = "untracked"
    UNPUSHED_COMMITS = "unpushed_commits"


class ModelDirtyConvergeDeclaration(BaseModel):
    """The ``dirty_converge`` section of the node contract, validated once."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    engine: str
    converge_script: str
    ticket: str = Field(pattern=r"^OMN-\d+$")
    lane: str
    tracking_branches: tuple[str, ...] = Field(min_length=1)
    max_listed: int = Field(ge=1)
    # Directories, relative to the registry root, that the deploy agent's image build holds for as long
    # as it stages and builds; a tick leaves every clone alone while one is live (OMN-22718).
    build_lock_dirs: tuple[str, ...] = Field(min_length=1)
    # A held lock older than this is no live build: the deploy agent bounds one image build at 3600 s.
    build_lock_max_age_seconds: int = Field(gt=3600)
    # The lab alarm condition a refused clone is raised under; the subject is the clone's path.
    alarm_condition: str = Field(min_length=1)

    @model_validator(mode="after")
    def _relative(self) -> Self:
        for path in (self.engine, self.converge_script):
            if path.startswith("/") or ".." in path.split("/"):
                raise ValueError(
                    "engine and converge_script are registry-relative paths"
                )
        if any(path.startswith("/") for path in self.build_lock_dirs):
            raise ValueError("build_lock_dirs are registry-relative paths")
        return self


class ModelLocalWork(BaseModel):
    """One kind of local work a clone holds: how many paths or commits, and the first few names."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: EnumLocalWorkKind
    count: int = Field(ge=1)
    names: tuple[str, ...] = ()


class ModelDirtyConvergeOutcome(BaseModel):
    """One clone the sync engine reported refused too long, and what the tick did with it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    clone: str
    status: EnumDirtyConvergeStatus
    reason: str = Field(default="", max_length=2000)
    refused_for_seconds: int = Field(ge=0)
    converged_to: str = ""
    #: What a REFUSED clone holds, by kind; empty for every other status.
    local_work: tuple[ModelLocalWork, ...] = ()
    #: How the owned alarm of a REFUSED clone ended: delivered, suppressed (an episode is already open),
    #: not-configured (no alarm delivery on this host) or failed: <why>. Empty for every other status.
    alarm: str = ""


class ModelDirtyConvergeTickRequest(BaseModel):
    """One tick over the registry root the handler is composed with; it carries no input."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelDirtyConvergeTickReceipt(BaseModel):
    """Every refused clone the tick acted on or left alone, with the age it applied."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    after_seconds: int = Field(ge=1)
    outcomes: tuple[ModelDirtyConvergeOutcome, ...] = ()


__all__ = [
    "EnumDirtyConvergeStatus",
    "EnumLocalWorkKind",
    "ModelDirtyConvergeDeclaration",
    "ModelDirtyConvergeOutcome",
    "ModelDirtyConvergeTickReceipt",
    "ModelDirtyConvergeTickRequest",
    "ModelLocalWork",
]
