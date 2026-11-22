# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Where the fill finds its work: three declared live sources, in goal order.

The tick's overlay declares each source's location and age bound; no path is written in code. A source set to ``null``
is declared absent on this host (the declaration says so; the pass does not skip it silently). A source that is declared
and cannot be read refuses the pass. At least one source must be declared, so a pass never has nothing to read.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


def _named_path(value: str) -> str:
    if not value or "\x00" in value or ".." in value.replace("\\", "/").split("/"):
        raise ValueError("path must be non-empty, hold no NUL and no '..' segment")
    return value


class ModelLabFillWorkSource(BaseModel):
    """A file source: where it is and how old (by its modification time) it may be."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    max_age_seconds: int = Field(ge=1)

    @model_validator(mode="after")
    def _path_stays_put(self) -> ModelLabFillWorkSource:
        _named_path(self.path)
        return self


class ModelLabFillWatcherSource(BaseModel):
    """The PR watcher's state file: where it is and the bounds of its two stamps (last tick and last full resync)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    max_tick_age_seconds: int = Field(ge=1)
    max_resync_age_seconds: int = Field(ge=1)

    @model_validator(mode="after")
    def _path_stays_put(self) -> ModelLabFillWatcherSource:
        _named_path(self.path)
        return self


class ModelLabFillWorkSources(BaseModel):
    """The three sources in goal order: NOT RUNNING ledger lines, red goal pull requests, open goal tickets."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    integration_ledger: ModelLabFillWorkSource | None
    pr_watcher: ModelLabFillWatcherSource | None
    goal_tickets: ModelLabFillWorkSource | None

    @model_validator(mode="after")
    def _one_is_declared(self) -> ModelLabFillWorkSources:
        if not (self.integration_ledger or self.pr_watcher or self.goal_tickets):
            raise ValueError("work_sources must declare at least one source")
        return self


__all__ = [
    "ModelLabFillWatcherSource",
    "ModelLabFillWorkSource",
    "ModelLabFillWorkSources",
]
