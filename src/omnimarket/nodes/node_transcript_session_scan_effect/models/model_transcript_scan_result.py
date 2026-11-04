# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Result of one transcript scan."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_transcript_session_scan_effect.models.model_transcript_manifest_entry import (
    ModelTranscriptManifestEntry,
)


class ModelTranscriptScanResult(BaseModel):
    """The manifest of the scanned roots and the counts that must agree with it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    entries: tuple[ModelTranscriptManifestEntry, ...]
    manifest_count: int = Field(ge=0)
    top_level_count: int = Field(ge=0)
    subagent_count: int = Field(ge=0)
    listed_transcript_count: int = Field(ge=0)
    root_transcript_counts: dict[str, int] = Field(default_factory=dict)
    archive_object_counts: dict[str, int] = Field(default_factory=dict)
    newest_archive_key: dict[str, str] = Field(default_factory=dict)
