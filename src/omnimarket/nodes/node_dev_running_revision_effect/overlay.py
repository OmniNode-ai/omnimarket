# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_dev_running_revision_effect: every setting of the running-revision reading, none in code.

Found at ``<root>/node_dev_running_revision_effect/overlay.yaml`` under a root named in
``ONEX_SKILL_OVERLAY_ROOTS``. Nothing here has a default: an absent key refuses the load and names it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.handlers.node_overlay_reader import load_structured_overlay

NODE_NAME = "node_dev_running_revision_effect"


class ModelProvenanceSshSettings(BaseModel):
    """How to reach the runtime's build provenance file: ssh to the host, docker exec into the service."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host_env: str = Field(min_length=1)
    compose_project: str = Field(min_length=1)
    service: str = Field(min_length=1)
    provenance_path: str = Field(min_length=1)
    timeout_seconds: int = Field(gt=0)


class ModelMergeGitSettings(BaseModel):
    """The git reads of the merge history: the timeout, the lookback and the merge cap."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    timeout_seconds: int = Field(gt=0)
    lookback_seconds: int = Field(gt=0)
    max_merges: int = Field(gt=0)


class ModelSourceRepository(BaseModel):
    """One repository whose running revision and merge history the source reads."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str = Field(min_length=1)
    clone_path_env: str = Field(min_length=1)
    ref: str = Field(min_length=1)
    provenance_section: Literal["siblings", "build_context"]


class ModelRunningRevisionSourceSettings(BaseModel):
    """The source's settings: the ssh route to the provenance file, the git bounds and the repositories."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ssh: ModelProvenanceSshSettings
    git: ModelMergeGitSettings
    repositories: tuple[ModelSourceRepository, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_repositories(self) -> ModelRunningRevisionSourceSettings:
        names = [repo.repository for repo in self.repositories]
        if len(names) != len(set(names)):
            raise ValueError("a repository is declared twice")
        return self


def load_settings(
    environ: Mapping[str, str] | None = None,
) -> ModelRunningRevisionSourceSettings:
    """Read ``<root>/node_dev_running_revision_effect/overlay.yaml``; absent refuses."""
    return load_structured_overlay(
        NODE_NAME, ModelRunningRevisionSourceSettings, environ
    )


__all__ = [
    "NODE_NAME",
    "ModelMergeGitSettings",
    "ModelProvenanceSshSettings",
    "ModelRunningRevisionSourceSettings",
    "ModelSourceRepository",
    "load_settings",
]
