# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2025 OmniNode Team
"""FilesystemCrawler handler: walks path prefixes and emits document lifecycle events.

Architecture:
    Document Ingestion Pipeline design (§5 Crawl State and Change Detection).

    Change Detection Strategy (two-stage):
        1. mtime fast-path: if stat.st_mtime is unchanged vs stored state, skip
           the file without re-reading or re-hashing it (most common case).
        2. If mtime changed: compute SHA-256(content). If the hash is also
           unchanged, update last_crawled_at_utc only (mtime bumped by an
           editor without content change). If the hash differs, emit
           document.changed.v1.
        3. Not in state table: emit document.discovered.v1.
        4. Records in state table but not found in walk: emit document.removed.v1.

Migrated from omnimemory to omnimarket for OMN-8299 (Wave 3).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

from omnimemory.enums.crawl.enum_context_source_type import EnumContextSourceType
from omnimemory.enums.crawl.enum_crawler_type import EnumCrawlerType
from omnimemory.enums.crawl.enum_detected_doc_type import EnumDetectedDocType
from omnimemory.models.crawl.model_crawl_state_record import ModelCrawlStateRecord
from omnimemory.models.crawl.model_document_changed_event import (
    ModelDocumentChangedEvent,
)
from omnimemory.models.crawl.model_document_discovered_event import (
    ModelDocumentDiscoveredEvent,
)
from omnimemory.models.crawl.model_document_indexed_event import (
    ModelDocumentIndexedEvent,
)
from omnimemory.models.crawl.model_document_removed_event import (
    ModelDocumentRemovedEvent,
)

from omnimarket.nodes.node_filesystem_crawler_effect.models.model_filesystem_crawl_result import (
    ModelFilesystemCrawlResult,
)

if TYPE_CHECKING:
    from omnimemory.models.crawl.types import TriggerSource
    from omnimemory.protocols.protocol_crawl_state_repository import (
        ProtocolCrawlStateRepository,
    )

    from omnimarket.nodes.node_filesystem_crawler_effect.models.model_filesystem_crawler_config import (
        ModelFilesystemCrawlerConfig,
    )

__all__ = ["HandlerFilesystemCrawler"]

logger = logging.getLogger(__name__)

HANDLER_ID_FILESYSTEM_CRAWLER: str = "filesystem-crawler"

DEFAULT_SCOPE_REF: str = "omninode/shared"

_STATIC_STANDARDS_PREFIXES: tuple[str, ...] = (str(Path("~/.claude").expanduser()),)

_SKIP_DIRS: frozenset[str] = frozenset({"src", "docs", "design", "plans", "handoffs"})


def _compute_sha256(content: bytes) -> str:
    """Return the lowercase hex SHA-256 digest of content."""
    return hashlib.sha256(content).hexdigest()


def _detect_doc_type(path: Path) -> EnumDetectedDocType:
    name = path.name
    name_upper = name.upper()
    parts_lower = [p.lower() for p in path.parts]

    if name == "CLAUDE.md":
        return EnumDetectedDocType.CLAUDE_MD

    if (
        name_upper == "DEEP_DIVE.MD"
        or name_upper.startswith("DEEP_DIVE_")
        or name_upper.endswith("_DEEP_DIVE.MD")
    ):
        return EnumDetectedDocType.DEEP_DIVE

    if name_upper.endswith(".MD") and (
        "ARCHITECTURE" in name_upper or "OVERVIEW" in name_upper
    ):
        return EnumDetectedDocType.ARCHITECTURE_DOC

    if name.upper() == "README.MD":
        return EnumDetectedDocType.README

    if "design" in parts_lower:
        return EnumDetectedDocType.DESIGN_DOC

    if "plans" in parts_lower or "plan" in parts_lower:
        return EnumDetectedDocType.PLAN

    if "handoffs" in parts_lower or "handoff" in parts_lower:
        return EnumDetectedDocType.HANDOFF

    return EnumDetectedDocType.UNKNOWN_MD


def _source_type_for_path(path: Path) -> EnumContextSourceType:
    for prefix in _STATIC_STANDARDS_PREFIXES:
        if path.is_relative_to(Path(prefix)):
            return EnumContextSourceType.STATIC_STANDARDS

    if path.name == "CLAUDE.md":
        return EnumContextSourceType.STATIC_STANDARDS

    return EnumContextSourceType.REPO_DERIVED


def _priority_hint_for_path(path: Path, path_prefixes: list[str]) -> int:
    name_upper = path.name.upper()
    parts_lower = [p.lower() for p in path.parts]

    for prefix in _STATIC_STANDARDS_PREFIXES:
        if path.is_relative_to(Path(prefix)) and path.name == "CLAUDE.md":
            return 95

    if path.name == "CLAUDE.md":
        return 85

    if "design" in parts_lower and (
        "ARCHITECTURE" in name_upper or "OVERVIEW" in name_upper
    ):
        return 80

    if "design" in parts_lower:
        return 70

    if "plans" in parts_lower or "plan" in parts_lower:
        return 65

    if "handoffs" in parts_lower or "handoff" in parts_lower:
        return 60

    if path.name.upper() == "README.MD" and str(path.parent) in path_prefixes:
        return 55

    if (
        name_upper == "DEEP_DIVE.MD"
        or name_upper.startswith("DEEP_DIVE_")
        or name_upper.endswith("_DEEP_DIVE.MD")
    ):
        return 45

    return 35


def _scope_ref_for_path(
    path: Path,
    scope_mappings: list[tuple[str, str]],
) -> str:
    best_prefix_len = -1
    best_scope = DEFAULT_SCOPE_REF

    for prefix, scope in scope_mappings:
        prefix_path = Path(prefix)
        prefix_parts_len = len(prefix_path.parts)
        if path.is_relative_to(prefix_path) and prefix_parts_len > best_prefix_len:
            best_prefix_len = prefix_parts_len
            best_scope = scope

    return best_scope


def _extract_tags(path: Path, doc_type: EnumDetectedDocType) -> list[str]:
    tags: list[str] = []
    tags.append(str(path))
    tags.append(f"doctype:{doc_type.value}")

    for part in reversed(path.parts[:-1]):
        if part not in _SKIP_DIRS and not part.startswith("."):
            tags.append(f"repo:{part}")
            break

    return tags


class HandlerFilesystemCrawler:
    """Walks configured path prefixes for .md files and emits crawl events."""

    def __init__(
        self,
        config: ModelFilesystemCrawlerConfig,
        crawl_state_repo: ProtocolCrawlStateRepository,
    ) -> None:
        self._config = config
        self._crawl_state_repo = crawl_state_repo

        logger.info(
            "HandlerFilesystemCrawler initialized",
            extra={
                "handler": HANDLER_ID_FILESYSTEM_CRAWLER,
                "path_prefixes": config.path_prefixes,
                "max_file_size_bytes": config.max_file_size_bytes,
            },
        )

    async def crawl(
        self,
        correlation_id: UUID,
        crawl_scope: str,
        trigger_source: TriggerSource,
        publish_callback: Callable[
            [str, dict[str, object]], Coroutine[object, object, None]
        ],
        scope_mappings: list[tuple[str, str]] | None = None,
    ) -> ModelFilesystemCrawlResult:
        """Execute a full filesystem crawl and emit lifecycle events."""
        resolved_mappings = scope_mappings or []
        now_utc = datetime.now(UTC)

        files_walked = 0
        discovered_count = 0
        changed_count = 0
        unchanged_count = 0
        skipped_count = 0
        mtime_skipped_count = 0
        indexed_count = 0
        error_count = 0
        truncated = False

        walked_paths: set[str] = set()
        scope_refs_seen: set[str] = set()

        for prefix_str in self._config.path_prefixes:
            if not Path(prefix_str).is_absolute():
                raise ValueError(f"path_prefix must be absolute: {prefix_str!r}")

        for prefix_str in self._config.path_prefixes:
            prefix_path = Path(prefix_str)
            if not await asyncio.to_thread(prefix_path.exists):
                logger.warning(
                    "Path prefix does not exist, skipping",
                    extra={
                        "handler": HANDLER_ID_FILESYSTEM_CRAWLER,
                        "prefix": prefix_str,
                        "correlation_id": str(correlation_id),
                    },
                )
                continue

            file_glob = self._config.file_glob

            # Default-argument capture prevents closure-over-loop-variable bug.
            def _rglob_prefix(
                _p: Path = prefix_path, _g: str = file_glob
            ) -> list[Path]:
                results: list[Path] = []
                try:
                    for entry in _p.rglob(_g):
                        results.append(entry)
                except OSError as exc:
                    logger.warning(
                        "OSError during rglob, partial results returned",
                        extra={
                            "handler": HANDLER_ID_FILESYSTEM_CRAWLER,
                            "prefix": str(_p),
                            "error": str(exc),
                            "error_type": type(exc).__name__,
                        },
                    )
                return results

            resolved_prefix_path = await asyncio.to_thread(prefix_path.resolve)

            for md_path in await asyncio.to_thread(_rglob_prefix):
                if files_walked >= self._config.max_files_per_crawl:
                    truncated = True
                    logger.warning(
                        "max_files_per_crawl reached, crawl truncated",
                        extra={
                            "handler": HANDLER_ID_FILESYSTEM_CRAWLER,
                            "limit": self._config.max_files_per_crawl,
                            "correlation_id": str(correlation_id),
                        },
                    )
                    break

                resolved_path = await asyncio.to_thread(md_path.resolve)

                if not resolved_path.is_relative_to(resolved_prefix_path):
                    logger.warning(
                        "Resolved path escapes crawl prefix (possible symlink"
                        " traversal), skipping",
                        extra={
                            "handler": HANDLER_ID_FILESYSTEM_CRAWLER,
                            "path": str(md_path),
                            "resolved_path": str(resolved_path),
                            "prefix": str(resolved_prefix_path),
                            "correlation_id": str(correlation_id),
                        },
                    )
                    skipped_count += 1
                    continue

                files_walked += 1

                abs_path_str = str(resolved_path)

                try:
                    stat = await asyncio.to_thread(md_path.stat)
                except OSError as exc:
                    logger.warning(
                        "Could not stat file, skipping",
                        extra={
                            "handler": HANDLER_ID_FILESYSTEM_CRAWLER,
                            "path": abs_path_str,
                            "error": str(exc),
                            "correlation_id": str(correlation_id),
                        },
                    )
                    error_count += 1
                    continue

                if stat.st_size > self._config.max_file_size_bytes:
                    logger.warning(
                        "File exceeds max_file_size_bytes, skipping",
                        extra={
                            "handler": HANDLER_ID_FILESYSTEM_CRAWLER,
                            "path": abs_path_str,
                            "size_bytes": stat.st_size,
                            "max_bytes": self._config.max_file_size_bytes,
                            "correlation_id": str(correlation_id),
                        },
                    )
                    skipped_count += 1
                    continue

                walked_paths.add(abs_path_str)

                scope_ref = _scope_ref_for_path(resolved_path, resolved_mappings)
                scope_refs_seen.add(scope_ref)

                prior_state = await self._crawl_state_repo.get_state(
                    source_ref=abs_path_str,
                    crawler_type=EnumCrawlerType.FILESYSTEM,
                    scope_ref=scope_ref,
                )

                current_mtime = stat.st_mtime
                if (
                    prior_state is not None
                    and prior_state.last_known_mtime is not None
                    and prior_state.last_known_mtime == current_mtime
                ):
                    updated_state = ModelCrawlStateRecord(
                        source_ref=abs_path_str,
                        crawler_type=EnumCrawlerType.FILESYSTEM,
                        scope_ref=scope_ref,
                        content_fingerprint=prior_state.content_fingerprint,
                        source_version=prior_state.source_version,
                        last_crawled_at_utc=now_utc,
                        last_changed_at_utc=prior_state.last_changed_at_utc,
                        last_known_mtime=current_mtime,
                    )
                    await self._crawl_state_repo.upsert_state(updated_state)
                    mtime_skipped_count += 1
                    continue

                content = await _read_file_async(md_path)
                if content is None:
                    error_count += 1
                    continue

                fingerprint = _compute_sha256(content)
                blob_ref = f"sha256:{fingerprint}"
                token_estimate = len(content.decode("utf-8", errors="replace")) // 4
                doc_type = _detect_doc_type(resolved_path)
                source_type = _source_type_for_path(resolved_path)
                priority = _priority_hint_for_path(
                    resolved_path, self._config.path_prefixes
                )
                tags = _extract_tags(resolved_path, doc_type)

                if prior_state is None:
                    discovered_event = ModelDocumentDiscoveredEvent(
                        correlation_id=correlation_id,
                        emitted_at_utc=now_utc,
                        crawler_type=EnumCrawlerType.FILESYSTEM,
                        crawl_scope=crawl_scope,
                        trigger_source=trigger_source,
                        source_ref=abs_path_str,
                        source_type=source_type,
                        source_version=None,
                        content_fingerprint=fingerprint,
                        content_blob_ref=blob_ref,
                        token_estimate=token_estimate,
                        scope_ref=scope_ref,
                        detected_doc_type=doc_type,
                        tags=tags,
                        priority_hint=priority,
                    )
                    await _publish_event(
                        publish_callback,
                        self._config.publish_topic_discovered,
                        discovered_event.model_dump(mode="json"),
                        correlation_id,
                    )
                    discovered_count += 1

                    indexed_event = ModelDocumentIndexedEvent(
                        correlation_id=correlation_id,
                        emitted_at_utc=now_utc,
                        crawler_type=EnumCrawlerType.FILESYSTEM,
                        crawl_scope=crawl_scope,
                        trigger_source=trigger_source,
                        source_ref=abs_path_str,
                        source_type=source_type,
                        content_fingerprint=fingerprint,
                        scope_ref=scope_ref,
                    )
                    await _publish_event(
                        publish_callback,
                        self._config.publish_topic_indexed,
                        indexed_event.model_dump(mode="json"),
                        correlation_id,
                    )
                    indexed_count += 1

                    new_state = ModelCrawlStateRecord(
                        source_ref=abs_path_str,
                        crawler_type=EnumCrawlerType.FILESYSTEM,
                        scope_ref=scope_ref,
                        content_fingerprint=fingerprint,
                        source_version=None,
                        last_crawled_at_utc=now_utc,
                        last_changed_at_utc=now_utc,
                        last_known_mtime=current_mtime,
                    )
                    await self._crawl_state_repo.upsert_state(new_state)

                elif prior_state.content_fingerprint == fingerprint:
                    unchanged_count += 1
                    updated_state = ModelCrawlStateRecord(
                        source_ref=abs_path_str,
                        crawler_type=EnumCrawlerType.FILESYSTEM,
                        scope_ref=scope_ref,
                        content_fingerprint=fingerprint,
                        source_version=prior_state.source_version,
                        last_crawled_at_utc=now_utc,
                        last_changed_at_utc=prior_state.last_changed_at_utc,
                        last_known_mtime=current_mtime,
                    )
                    await self._crawl_state_repo.upsert_state(updated_state)

                else:
                    changed_event = ModelDocumentChangedEvent(
                        correlation_id=correlation_id,
                        emitted_at_utc=now_utc,
                        crawler_type=EnumCrawlerType.FILESYSTEM,
                        crawl_scope=crawl_scope,
                        trigger_source=trigger_source,
                        source_ref=abs_path_str,
                        source_type=source_type,
                        source_version=None,
                        content_fingerprint=fingerprint,
                        content_blob_ref=blob_ref,
                        token_estimate=token_estimate,
                        scope_ref=scope_ref,
                        detected_doc_type=doc_type,
                        tags=tags,
                        priority_hint=priority,
                        previous_content_fingerprint=prior_state.content_fingerprint,
                        previous_source_version=prior_state.source_version,
                    )
                    await _publish_event(
                        publish_callback,
                        self._config.publish_topic_changed,
                        changed_event.model_dump(mode="json"),
                        correlation_id,
                    )
                    changed_count += 1

                    indexed_event = ModelDocumentIndexedEvent(
                        correlation_id=correlation_id,
                        emitted_at_utc=now_utc,
                        crawler_type=EnumCrawlerType.FILESYSTEM,
                        crawl_scope=crawl_scope,
                        trigger_source=trigger_source,
                        source_ref=abs_path_str,
                        source_type=source_type,
                        content_fingerprint=fingerprint,
                        scope_ref=scope_ref,
                    )
                    await _publish_event(
                        publish_callback,
                        self._config.publish_topic_indexed,
                        indexed_event.model_dump(mode="json"),
                        correlation_id,
                    )
                    indexed_count += 1

                    updated_state = ModelCrawlStateRecord(
                        source_ref=abs_path_str,
                        crawler_type=EnumCrawlerType.FILESYSTEM,
                        scope_ref=scope_ref,
                        content_fingerprint=fingerprint,
                        source_version=None,
                        last_crawled_at_utc=now_utc,
                        last_changed_at_utc=now_utc,
                        last_known_mtime=current_mtime,
                    )
                    await self._crawl_state_repo.upsert_state(updated_state)

            if truncated:
                break

        if truncated:
            logger.warning(
                "crawl truncated at max_files_per_crawl; skipping removal"
                " detection to avoid spurious removals",
                extra={
                    "handler": HANDLER_ID_FILESYSTEM_CRAWLER,
                    "max_files_per_crawl": self._config.max_files_per_crawl,
                    "correlation_id": str(correlation_id),
                },
            )
            removed_count = 0
        else:
            removed_count = await self._detect_and_emit_removals(
                walked_paths=walked_paths,
                scope_refs_seen=scope_refs_seen,
                correlation_id=correlation_id,
                emitted_at_utc=now_utc,
                crawl_scope=crawl_scope,
                trigger_source=trigger_source,
                publish_callback=publish_callback,
                resolved_mappings=resolved_mappings,
            )

        result = ModelFilesystemCrawlResult(
            files_walked=files_walked,
            discovered_count=discovered_count,
            changed_count=changed_count,
            unchanged_count=unchanged_count,
            skipped_count=skipped_count,
            mtime_skipped_count=mtime_skipped_count,
            indexed_count=indexed_count,
            removed_count=removed_count,
            error_count=error_count,
            truncated=truncated,
        )

        logger.info(
            "Filesystem crawl complete",
            extra={
                "handler": HANDLER_ID_FILESYSTEM_CRAWLER,
                "correlation_id": str(correlation_id),
                "files_walked": files_walked,
                "discovered": discovered_count,
                "changed": changed_count,
                "unchanged": unchanged_count,
                "skipped": skipped_count,
                "indexed": indexed_count,
                "removed": removed_count,
                "errors": error_count,
                "truncated": truncated,
            },
        )

        return result

    async def _detect_and_emit_removals(
        self,
        walked_paths: set[str],
        scope_refs_seen: set[str],
        correlation_id: UUID,
        emitted_at_utc: datetime,
        crawl_scope: str,
        trigger_source: TriggerSource,
        publish_callback: Callable[
            [str, dict[str, object]], Coroutine[object, object, None]
        ],
        resolved_mappings: list[tuple[str, str]],
    ) -> int:
        """Emit document-removed events for state records no longer on disk."""
        removed_count = 0

        if scope_refs_seen:
            affected_scopes = set(scope_refs_seen)
            for prefix_str in self._config.path_prefixes:
                prefix_path = Path(prefix_str)
                if (
                    _scope_ref_for_path(prefix_path, resolved_mappings)
                    == DEFAULT_SCOPE_REF
                ):
                    affected_scopes.add(DEFAULT_SCOPE_REF)
                    break
        else:
            affected_scopes = set()
            for prefix_str in self._config.path_prefixes:
                prefix_path = Path(prefix_str)
                scope = _scope_ref_for_path(prefix_path, resolved_mappings)
                affected_scopes.add(scope)

        for scope_ref in affected_scopes:
            known_records = await self._crawl_state_repo.list_states_for_scope(
                crawler_type=EnumCrawlerType.FILESYSTEM,
                scope_ref=scope_ref,
            )

            for record in known_records:
                if record.source_ref not in walked_paths:
                    source_type = _source_type_for_path(Path(record.source_ref))
                    removed_event = ModelDocumentRemovedEvent(
                        correlation_id=correlation_id,
                        emitted_at_utc=emitted_at_utc,
                        crawler_type=EnumCrawlerType.FILESYSTEM,
                        crawl_scope=crawl_scope,
                        trigger_source=trigger_source,
                        source_ref=record.source_ref,
                        source_type=source_type,
                        scope_ref=scope_ref,
                        last_known_content_fingerprint=record.content_fingerprint,
                        last_known_source_version=record.source_version,
                    )
                    await _publish_event(
                        publish_callback,
                        self._config.publish_topic_removed,
                        removed_event.model_dump(mode="json"),
                        correlation_id,
                    )
                    await self._crawl_state_repo.delete_state(
                        source_ref=record.source_ref,
                        crawler_type=EnumCrawlerType.FILESYSTEM,
                        scope_ref=scope_ref,
                    )
                    removed_count += 1

        return removed_count


async def _read_file_async(path: Path) -> bytes | None:
    try:
        return await asyncio.to_thread(path.read_bytes)
    except OSError as exc:
        logger.warning(
            "Failed to read file content",
            extra={"path": str(path), "error": str(exc)},
        )
        return None


async def _publish_event(
    publish_callback: Callable[
        [str, dict[str, object]], Coroutine[object, object, None]
    ],
    topic: str,
    payload: dict[str, object],
    correlation_id: UUID,
) -> None:
    try:
        await publish_callback(topic, payload)
    except Exception as exc:
        logger.error(
            "Failed to publish crawl event",
            extra={
                "topic": topic,
                "correlation_id": str(correlation_id),
                "error": str(exc),
                "error_type": type(exc).__name__,
            },
        )
