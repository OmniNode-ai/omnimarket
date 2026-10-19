# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The frozen review replay set's manifest, shared by the nodes that write and read it (OMN-20910)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelReplayItem(BaseModel):
    """One frozen diff."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    item_id: str
    repo: str
    pr_number: int
    merge_sha: str
    parent_sha: str
    title: str
    changed_lines: int
    size_bin: str
    diff_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    diff_file: str
    pinned: bool = False


class ModelReplayManifest(BaseModel):
    """The frozen set; its canonical-JSON SHA-256 is the manifest hash."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = "review_replay_set.v1"
    seed: str
    target_count: int
    size_bin_upper_lines: tuple[int, int]
    items: tuple[ModelReplayItem, ...]


__all__ = ["ModelReplayItem", "ModelReplayManifest"]
