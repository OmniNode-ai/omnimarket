# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The request and result of node_host_install_file_effect (OMN-23957)."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ModelHostInstallFileRequest(BaseModel):
    """Names the host, unit, mode, and the ordered file paths of the unit."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    host: str = Field(min_length=1)
    unit: str = Field(min_length=1)
    mode: Literal["snapshot", "read", "restore"]
    paths: tuple[str, ...] = Field(min_length=1)

    @field_validator("paths")
    @classmethod
    def _paths_are_absolute(cls, paths: tuple[str, ...]) -> tuple[str, ...]:
        for path in paths:
            if not path.startswith("/"):
                raise ValueError(f"path is not absolute: {path!r}")
        return paths


class ModelHostInstallFileResult(BaseModel):
    """The step result: status, presence, text, digest, backup directory, and detail."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    host: str
    unit: str
    mode: Literal["snapshot", "read", "restore"]
    status: Literal["success", "failure"]
    present: bool
    text: str
    digest: str
    backup_dir: str
    detail: str
