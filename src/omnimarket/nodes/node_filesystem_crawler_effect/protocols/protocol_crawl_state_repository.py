# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2025 OmniNode Team
"""Protocol for the crawl state repository used by all crawlers.

OMN-15238: relocated verbatim from
``omnimemory.protocols.protocol_crawl_state_repository``, deleted by OMN-14618
(omnimemory#410) on an intra-repo-only orphan grep that missed omnimarket's live
consumer. See the sibling ``models.model_crawl_state_record`` docstring for the
full provenance.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from omnimemory.enums.crawl.enum_crawler_type import EnumCrawlerType

    from omnimarket.nodes.node_filesystem_crawler_effect.models.model_crawl_state_record import (
        ModelCrawlStateRecord,
    )


@runtime_checkable
class ProtocolCrawlStateRepository(Protocol):
    """Read/write interface for the omnimemory_crawl_state table.

    Each crawler type (FilesystemCrawler, GitRepoCrawler, LinearCrawler)
    uses this protocol to load prior crawl state, update state after a
    successful crawl, and delete state for removed documents.

    All methods are independent transactions — the adapter does not support
    external transaction control.
    """

    async def get_state(
        self,
        source_ref: str,
        crawler_type: EnumCrawlerType,
        scope_ref: str,
    ) -> ModelCrawlStateRecord | None:
        """Fetch the crawl state record for a specific document.

        Args:
            source_ref: Absolute path, URL, or Linear ID.
            crawler_type: Identifies which crawler owns the record.
            scope_ref: Scope assignment for the document.

        Returns:
            The crawl state record if one exists; None otherwise.
        """
        ...

    async def list_states_for_scope(
        self,
        crawler_type: EnumCrawlerType,
        scope_ref: str,
    ) -> list[ModelCrawlStateRecord]:
        """List all crawl state records for a crawler type + scope combination.

        Used by crawlers at the end of a walk to detect removed documents
        (records in state table that were not found in the filesystem walk).

        Args:
            crawler_type: Filters records to a specific crawler.
            scope_ref: Filters records to a specific scope.

        Returns:
            All records matching the (crawler_type, scope_ref) combination.
        """
        ...

    async def upsert_state(self, record: ModelCrawlStateRecord) -> None:
        """Insert or update a crawl state record.

        Creates the record if none exists for (source_ref, crawler_type,
        scope_ref), or updates the existing record atomically.

        Args:
            record: The new or updated state record.
        """
        ...

    async def delete_state(
        self,
        source_ref: str,
        crawler_type: EnumCrawlerType,
        scope_ref: str,
    ) -> None:
        """Remove the crawl state record for a document.

        Called when a document is confirmed removed (FilesystemCrawler:
        file no longer found in walk; LinearCrawler: issue absent from
        list response).

        Args:
            source_ref: Absolute path, URL, or Linear ID.
            crawler_type: Identifies which crawler owns the record.
            scope_ref: Scope assignment for the document.
        """
        ...
