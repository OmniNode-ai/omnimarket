# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-owned repository retirement policy and scan findings."""

from pathlib import PurePosixPath
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

RepositoryName = Annotated[
    str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
]


class ModelRepositoryRetirementOverlay(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    additional_retired_repositories: tuple[RepositoryName, ...] = ()


class ModelRepositoryRetirementPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    retired_repositories: tuple[RepositoryName, ...] = Field(min_length=1)
    backup_directory: str = Field(min_length=1)
    sync_directories: tuple[str, ...] = Field(min_length=1)
    source_sync_glob: str = Field(min_length=1)
    registry_file_globs: tuple[str, ...] = Field(min_length=1)
    registry_key_pattern: str = Field(min_length=1)
    registry_constant_pattern: str = Field(min_length=1)

    @field_validator("backup_directory")
    @classmethod
    def relative_backup(cls, value: str) -> str:
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or path == PurePosixPath("."):
            raise ValueError("backup_directory must stay beneath the workspace")
        return value


class ModelRetiredRepositoryFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    repository: str
    kind: str
