# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed legacy dispatch queue item for ``.onex_state/dispatch_queue`` YAML."""

from __future__ import annotations

import re
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

_REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,96}$")
_TICKET_PATTERN = re.compile(r"^[A-Z]+-[0-9]+$")
_WORKER_ROLES = frozenset(
    {"watcher", "fixer", "designer", "auditor", "synthesizer", "sweep", "ops"}
)


class ModelDispatchQueueItem(BaseModel):
    """Legacy YAML queue item that can be compiled into a dispatch-worker command."""

    model_config = ConfigDict(frozen=True, extra="allow")

    name: str = Field(..., description="Worker handle.")
    team: str = Field(..., description="Team name.")
    role: str = Field(..., description="Dispatch worker role.")
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

    @field_validator("role")
    @classmethod
    def validate_role(cls, value: str) -> str:
        normalized = value.strip()
        if normalized not in _WORKER_ROLES:
            allowed = ", ".join(sorted(_WORKER_ROLES))
            raise ValueError(f"role must be one of: {allowed}")
        return normalized

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


__all__: list[str] = ["ModelDispatchQueueItem"]
