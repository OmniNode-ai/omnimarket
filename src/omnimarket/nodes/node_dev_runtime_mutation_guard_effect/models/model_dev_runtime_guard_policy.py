# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The guard_policy block of the node contract, typed (OMN-21795).

Model ownership: PRIVATE to omnimarket.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelProtectedProject(BaseModel):
    """One compose project only a deployment or a grant may mutate."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    project: str = Field(min_length=1, description="The compose project name.")
    description: str = Field(
        default="", description="What the project is, for refusals."
    )
    compose_files: tuple[str, ...] = Field(
        default=(),
        description="Compose file basenames whose top-level name is this project.",
    )
    containers: tuple[str, ...] = Field(
        default=(), description="The fixed container names the project declares."
    )


class ModelDevRuntimeGrantPolicy(BaseModel):
    """What a live grant row on the rolling work ledger looks like."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    row_kinds: tuple[str, ...] = Field(min_length=1)
    target_cell: str = Field(min_length=1)
    target_prefix: str = Field(default="compose:")
    until_cell: str = Field(min_length=1)
    max_grant_hours: float = Field(gt=0)
    ledger_path_env: str = Field(min_length=1)
    tail_bytes: int = Field(gt=0)


class ModelDevRuntimeGuardPolicy(BaseModel):
    """The whole guard_policy block."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    protected_projects: tuple[ModelProtectedProject, ...] = Field(min_length=1)
    compose_mutating_verbs: tuple[str, ...] = Field(min_length=1)
    docker_mutating_verbs: tuple[str, ...] = Field(min_length=1)
    grant: ModelDevRuntimeGrantPolicy
    deployment_route: str = Field(min_length=1)

    def project(self, name: str) -> ModelProtectedProject | None:
        """The protected project of that name, else None."""
        return next((p for p in self.protected_projects if p.project == name), None)
