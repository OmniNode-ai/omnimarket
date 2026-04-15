# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2025 OmniNode Team
"""Result model for a single filesystem crawl run."""

from pydantic import BaseModel, ConfigDict, Field


class ModelFilesystemCrawlResult(BaseModel):
    """Summary of a completed filesystem crawl run."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    files_walked: int = Field(..., ge=0)
    discovered_count: int = Field(..., ge=0)
    changed_count: int = Field(..., ge=0)
    unchanged_count: int = Field(..., ge=0)
    skipped_count: int = Field(..., ge=0)
    indexed_count: int = Field(default=0, ge=0)
    removed_count: int = Field(..., ge=0)
    error_count: int = Field(..., ge=0)
    mtime_skipped_count: int = Field(default=0, ge=0)
    truncated: bool = Field(default=False)
