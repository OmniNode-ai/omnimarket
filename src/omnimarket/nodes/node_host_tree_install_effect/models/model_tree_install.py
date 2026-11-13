# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Caller-owned roots, declarative layout, and a content-addressed tree receipt."""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict


class ModelTreeInstallOverlay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source_relative: str
    destination_relative: str


class ModelTreeInstallRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source_root: Path
    install_root: Path
    mode: Literal["install", "dry-run", "verify"] = "install"
    overlay: ModelTreeInstallOverlay | None = None


class ModelTreeInstallResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal[
        "installed", "unchanged", "verified", "dry-run", "refused", "failed", "locked"
    ]
    source: str = ""
    destination: str = ""
    tree_sha256: str = ""
    file_count: int = 0
    previous_tree: str = ""
    detail: str = ""
