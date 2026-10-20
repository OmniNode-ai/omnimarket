# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Explicit deployment source and options; credentials stay on the effect host."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ModelRuntimeDeployOptions(BaseModel):
    """The host deployment's existing option set, rendered as discrete argv."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    execute: bool = False
    force: bool = False
    restart: bool = False
    cold: bool = False
    profile: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_-]+$")
    print_compose_command: bool = False
    readback_only: bool = False
    prod: bool = False
    lock_timeout: int | None = Field(default=None, ge=0)
    effects_plan: str | None = Field(default=None, min_length=1)
    show_help: bool = False

    def to_arguments(self) -> tuple[str, ...]:
        """Keep argument boundaries even for a plan path with shell metacharacters."""
        args: list[str] = []
        for enabled, name in (
            (self.execute, "--execute"),
            (self.force, "--force"),
            (self.restart, "--restart"),
            (self.cold, "--cold"),
            (self.print_compose_command, "--print-compose-cmd"),
            (self.readback_only, "--readback-only"),
            (self.prod, "--prod"),
            (self.show_help, "--help"),
        ):
            if enabled:
                args.append(name)
        if self.profile is not None:
            args.extend(("--profile", self.profile))
        if self.lock_timeout is not None:
            args.extend(("--lock-timeout", str(self.lock_timeout)))
        if self.effects_plan is not None:
            args.extend(("--effects-plan", self.effects_plan))
        return tuple(args)


class ModelRuntimeDeployRequest(BaseModel):
    """Host-local deployment command. Never accepts arbitrary environment overrides."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    correlation_id: UUID
    repository_root: Path
    options: ModelRuntimeDeployOptions = Field(
        default_factory=ModelRuntimeDeployOptions
    )

    @field_validator("repository_root")
    @classmethod
    def absolute_source(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("repository_root must be absolute")
        return value
