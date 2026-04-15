# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2025 OmniNode Team
"""Configuration model for FilesystemCrawlerEffect handler."""

from pydantic import BaseModel, ConfigDict, Field

TOPIC_DOCUMENT_CHANGED = "onex.evt.omnimemory.document-changed.v1"
TOPIC_DOCUMENT_DISCOVERED = "onex.evt.omnimemory.document-discovered.v1"
TOPIC_DOCUMENT_INDEXED = "onex.evt.omnimemory.document-indexed.v1"
TOPIC_DOCUMENT_REMOVED = "onex.evt.omnimemory.document-removed.v1"


class ModelFilesystemCrawlerConfig(BaseModel):
    """Configuration for HandlerFilesystemCrawler."""

    model_config = ConfigDict(
        frozen=True, extra="forbid", strict=True, from_attributes=True
    )

    path_prefixes: list[str] = Field(default_factory=list)
    file_glob: str = Field(default="*.md")
    publish_topic_discovered: str = Field(default=TOPIC_DOCUMENT_DISCOVERED)
    publish_topic_changed: str = Field(default=TOPIC_DOCUMENT_CHANGED)
    publish_topic_removed: str = Field(default=TOPIC_DOCUMENT_REMOVED)
    publish_topic_indexed: str = Field(default=TOPIC_DOCUMENT_INDEXED)
    max_file_size_bytes: int = Field(default=5_242_880, ge=1)
    max_files_per_crawl: int = Field(default=10_000, ge=1)
