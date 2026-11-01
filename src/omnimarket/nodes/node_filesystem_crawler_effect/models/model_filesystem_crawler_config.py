# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2025 OmniNode Team
"""Configuration model for FilesystemCrawlerEffect handler."""

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.contract_topics import contract_topic

# Topic bindings from contract.yaml event_bus.publish_topics
_CONTRACT = Path(__file__).resolve().parent.parent / "contract.yaml"
TOPIC_DOCUMENT_CHANGED = contract_topic(
    _CONTRACT, "publish_topics", "document-changed.v1"
)
TOPIC_DOCUMENT_DISCOVERED = contract_topic(
    _CONTRACT, "publish_topics", "document-discovered.v1"
)
TOPIC_DOCUMENT_INDEXED = contract_topic(
    _CONTRACT, "publish_topics", "document-indexed.v1"
)
TOPIC_DOCUMENT_REMOVED = contract_topic(
    _CONTRACT, "publish_topics", "document-removed.v1"
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
