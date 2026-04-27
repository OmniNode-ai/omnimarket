# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelCrawlFilesystemResult — summary of a content-type-aware crawl run."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_filesystem_crawler_effect.models.model_content_discovered_event import (
    ModelContentDiscoveredEvent,
)


class ModelCrawlFilesystemResult(BaseModel):
    """Summary and collected events from a single content-type-aware crawl."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    files_scanned: int = Field(..., ge=0)
    events: tuple[ModelContentDiscoveredEvent, ...] = Field(default_factory=tuple)
    skipped_count: int = Field(default=0, ge=0)
    error_count: int = Field(default=0, ge=0)
    truncated: bool = Field(default=False)


__all__ = ["ModelCrawlFilesystemResult"]
