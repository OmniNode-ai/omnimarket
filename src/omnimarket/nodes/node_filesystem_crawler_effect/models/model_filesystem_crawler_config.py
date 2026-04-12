# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2025 OmniNode Team
"""Configuration model for FilesystemCrawlerEffect handler."""

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_filesystem_crawler_effect.topics import (
    TOPIC_DOCUMENT_CHANGED,
    TOPIC_DOCUMENT_DISCOVERED,
    TOPIC_DOCUMENT_INDEXED,
    TOPIC_DOCUMENT_REMOVED,
)


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
