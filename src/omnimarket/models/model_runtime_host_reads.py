# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Overlay of the two reads the factory floor makes inside the runtime host's containers (OMN-22549).

The fleet observation and the build provenance live in different service containers of the runtime host. Which
container holds which file is a deployment fact, so it is an overlay value and not code: the overlay
``runtime_host_reads`` names the ssh target variable, the compose project, the service holding each file, the
provenance file's path and, per repository, where in that file its deployed ref is and where its clone is.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelRuntimeRepoRef(BaseModel):
    """Where one repository's deployed ref sits in the provenance file, and where its clone is under the registry home."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    clone_relpath: str = Field(pattern=r"^[A-Za-z0-9_./-]+$")
    ref_path: tuple[str, ...] = Field(min_length=1)


class ModelRuntimeHostReadOverlay(BaseModel):
    """The container identities and the provenance layout the floor reads over ssh."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ssh_host_env: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    compose_project: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    fleet_service: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    provenance_service: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    provenance_path: str = Field(pattern=r"^/[A-Za-z0-9_./-]+$")
    repo_refs: tuple[ModelRuntimeRepoRef, ...] = Field(min_length=1)
    timeout_seconds: int = Field(default=30, gt=0)
