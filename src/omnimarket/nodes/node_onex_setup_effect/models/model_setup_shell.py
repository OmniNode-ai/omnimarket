# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Versioned survey policy and redacted shell conflicts."""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelSetupProfileSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    path: Path
    content: bytes = Field(repr=False)
    mode: int
    device: int
    inode: int


class ModelSetupShellPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal[1]
    profiles: dict[str, Literal["/bin/bash", "/bin/zsh"]]
    legacy_variables: tuple[str, ...]
    workspace_variables: tuple[str, ...]
    legacy_source_fragment: str
    set_aside_marker: str


class ModelSetupShellConflict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: Literal["profile", "environment", "executable", "unsafe-profile"]
    location: str
    line: int | None = Field(default=None, ge=1)
    name: str
    recoverable: bool = False
