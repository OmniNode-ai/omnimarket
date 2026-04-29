# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed legacy dispatch queue item for ``.onex_state/dispatch_queue`` YAML."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnimarket.nodes.node_dispatch_worker.models.model_dispatch_worker_command import (
    EnumWorkerRole,
    ModelDispatchWorkerCommand,
)

_REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,96}$")
_TICKET_PATTERN = re.compile(r"^[A-Z]+-[0-9]+$")


class ModelDispatchQueueItem(BaseModel):
    """Legacy YAML queue item that can be compiled into a dispatch-worker command."""

    model_config = ConfigDict(frozen=True, extra="allow")

    name: str = Field(..., description="Worker handle.")
    team: str = Field(..., description="Team name.")
    role: EnumWorkerRole = Field(..., description="Dispatch worker role.")
    scope: str = Field(..., description="Goal description.")
    targets: list[str] = Field(..., description="Tickets/PRs/paths this worker owns.")
    collision_fences: list[str] = Field(default_factory=list)
    reports_to: str = Field(default="team-lead")
    wall_clock_cap_min: int | None = Field(default=None, ge=5, le=480)
    model: str = Field(default="sonnet")
    replace: bool = Field(default=False)
    repo: str | None = Field(
        default=None,
        description="Optional canonical repo name used for missing-repo blocking.",
    )
    enqueued_at: datetime | None = None
    source: str = Field(default="legacy_dispatch_queue")

    @field_validator("scope")
    @classmethod
    def validate_scope(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("scope must be a non-empty string")
        return normalized

    @field_validator("targets")
    @classmethod
    def validate_targets(cls, value: list[str]) -> list[str]:
        targets = [target.strip() for target in value if target.strip()]
        if not targets:
            raise ValueError("targets must contain at least one non-empty entry")
        return targets

    @field_validator("repo")
    @classmethod
    def validate_repo(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            return None
        if not _REPO_PATTERN.match(normalized):
            raise ValueError("repo must be a simple repository directory name")
        return normalized

    @property
    def resolved_repo(self) -> str | None:
        """Return the repo explicitly declared or inferred from targets."""
        if self.repo:
            return self.repo
        for target in self.targets:
            candidate = target.split("#", maxsplit=1)[0].strip()
            if candidate and not _TICKET_PATTERN.fullmatch(candidate):
                return candidate
        return None

    def to_dispatch_worker_command(self) -> ModelDispatchWorkerCommand:
        """Convert the queue item to the existing compile-only worker command."""
        payload: dict[str, Any] = {
            "name": self.name,
            "team": self.team,
            "role": self.role,
            "scope": self.scope,
            "targets": self.targets,
            "collision_fences": self.collision_fences,
            "reports_to": self.reports_to,
            "wall_clock_cap_min": self.wall_clock_cap_min,
            "model": self.model,
            "replace": self.replace,
        }
        return ModelDispatchWorkerCommand(**payload)


__all__: list[str] = ["ModelDispatchQueueItem"]
