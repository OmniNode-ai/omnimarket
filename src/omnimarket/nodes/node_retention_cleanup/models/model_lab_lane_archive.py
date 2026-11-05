# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Verified host-local history before retiring a recoverable lane checkout."""

from pathlib import Path

from pydantic import BaseModel, ConfigDict


class ModelLabLaneArchiveRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    root: Path
    run: Path
    context: Path
    archive_subdir: str
    head: str = ""
    git_common_dir: str = ""


class ModelLabLaneArchiveResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    files_archived: int = 0
    bytes_archived: int = 0
