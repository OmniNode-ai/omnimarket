# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A hash-pinned manifest of files to place, with its expected per-folder counts."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelKbManifestFile(BaseModel):
    """One manifest file: its name, its group and its pinned content hash."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    group: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ModelKbManifest(BaseModel):
    """The manifest files and the per-folder-key counts they must resolve to."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    files: tuple[ModelKbManifestFile, ...]
    expected_counts: dict[str, int]


__all__ = ["ModelKbManifest", "ModelKbManifestFile"]
