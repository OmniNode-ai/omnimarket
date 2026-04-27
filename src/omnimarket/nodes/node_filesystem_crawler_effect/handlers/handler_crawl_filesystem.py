# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerCrawlFilesystem — content-type-aware directory walker.

Walks configured root paths or an explicit file list, detects content type by
extension, and emits one ``ModelContentDiscoveredEvent`` per file.

Content-type mapping (extension-based, not MIME / magic bytes):
    ``.py``          → python
    ``.ts`` / ``.tsx`` → typescript
    ``.js`` / ``.jsx`` → javascript
    ``contract.yaml`` → contract
    ``*.yaml``/``*.yml`` → yaml
    ``.md``          → markdown
    everything else  → unknown

Excludes: ``__pycache__``, ``.git``, ``node_modules``, ``.venv`` (configurable).
No imports from omnimemory, omniintelligence, or omnibase_compat.
Paths come from the input model — never read from OMNI_HOME or env vars.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from omnimarket.nodes.node_filesystem_crawler_effect.models.model_content_discovered_event import (
    ModelContentDiscoveredEvent,
)
from omnimarket.nodes.node_filesystem_crawler_effect.models.model_crawl_filesystem_request import (
    ModelCrawlFilesystemRequest,
)
from omnimarket.nodes.node_filesystem_crawler_effect.models.model_crawl_filesystem_result import (
    ModelCrawlFilesystemResult,
)

__all__ = ["HandlerCrawlFilesystem"]

logger = logging.getLogger(__name__)

_EXTENSION_MAP: dict[str, str] = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".md": "markdown",
    ".json": "json",
    ".toml": "toml",
    ".cfg": "ini",
    ".ini": "ini",
    ".rs": "rust",
    ".go": "go",
    ".sql": "sql",
}

CONTRACT_YAML_FILENAME = "contract.yaml"

_MAX_FILES_DEFAULT = 10_000


def detect_content_type(path: Path) -> str:
    """Return content type string based on file extension.

    ``contract.yaml`` is special-cased to ``contract``; other ``.yaml``/``.yml``
    files map to ``yaml``.
    """
    if path.name == CONTRACT_YAML_FILENAME:
        return "contract"
    return _EXTENSION_MAP.get(path.suffix.lower(), "unknown")


def _compute_sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _should_skip_dir(dir_name: str, exclude_dirs: frozenset[str]) -> bool:
    return dir_name in exclude_dirs


class HandlerCrawlFilesystem:
    """Content-type-aware filesystem crawler.

    Parameters
    ----------
    max_files : int
        Safety cap on total files scanned across all root paths.
    """

    def __init__(self, *, max_files: int = _MAX_FILES_DEFAULT) -> None:
        self._max_files = max_files

    async def crawl(
        self,
        request: ModelCrawlFilesystemRequest,
        correlation_id: UUID | None = None,
    ) -> ModelCrawlFilesystemResult:
        """Execute the crawl and return collected events.

        Dispatches to directory-walk or file-list mode based on the request.
        """
        corr = correlation_id or uuid4()
        exclude = frozenset(request.exclude_dirs)
        events: list[ModelContentDiscoveredEvent] = []
        files_scanned = 0
        skipped_count = 0
        error_count = 0
        truncated = False

        if request.file_list:
            (
                files_scanned,
                skipped_count,
                error_count,
                truncated,
                events,
            ) = await self._crawl_file_list(
                request,
                corr,
                exclude,
                events,
                files_scanned,
                skipped_count,
                error_count,
            )
        else:
            for root_path_str in request.root_paths:
                root = Path(root_path_str)
                if not await asyncio.to_thread(root.is_dir):
                    logger.warning(
                        "root_path is not a directory, skipping: %s", root_path_str
                    )
                    continue

                (
                    files_scanned,
                    skipped_count,
                    error_count,
                    truncated,
                    events,
                ) = await self._walk_root(
                    root,
                    request,
                    corr,
                    exclude,
                    events,
                    files_scanned,
                    skipped_count,
                    error_count,
                )
                if truncated:
                    break

        return ModelCrawlFilesystemResult(
            files_scanned=files_scanned,
            events=tuple(events),
            skipped_count=skipped_count,
            error_count=error_count,
            truncated=truncated,
        )

    async def _walk_root(
        self,
        root: Path,
        request: ModelCrawlFilesystemRequest,
        correlation_id: UUID,
        exclude: frozenset[str],
        events: list[ModelContentDiscoveredEvent],
        files_scanned: int,
        skipped_count: int,
        error_count: int,
    ) -> tuple[int, int, int, bool, list[ModelContentDiscoveredEvent]]:
        truncated = False

        def _iter() -> list[Path]:
            collected: list[Path] = []
            try:
                for entry in root.rglob("*"):
                    if entry.is_dir():
                        continue
                    if any(part in exclude for part in entry.parts):
                        continue
                    collected.append(entry)
            except OSError as exc:
                logger.warning("OSError during rglob on %s: %s", root, exc)
            return collected

        all_files = await asyncio.to_thread(_iter)

        for file_path in all_files:
            if files_scanned >= self._max_files:
                truncated = True
                logger.warning("max_files cap reached during walk of %s", root)
                break

            result = await self._process_file(
                file_path,
                root,
                request,
                correlation_id,
            )
            if result is None:
                error_count += 1
                continue

            event, did_skip = result
            if did_skip:
                skipped_count += 1
                continue

            files_scanned += 1
            if event is not None:
                events.append(event)

        return files_scanned, skipped_count, error_count, truncated, events

    async def _crawl_file_list(
        self,
        request: ModelCrawlFilesystemRequest,
        correlation_id: UUID,
        exclude: frozenset[str],
        events: list[ModelContentDiscoveredEvent],
        files_scanned: int,
        skipped_count: int,
        error_count: int,
    ) -> tuple[int, int, int, bool, list[ModelContentDiscoveredEvent]]:
        truncated = False

        for file_str in request.file_list:
            if files_scanned >= self._max_files:
                truncated = True
                break

            file_path = Path(file_str)
            if not await asyncio.to_thread(file_path.is_file):
                logger.warning("file_list entry does not exist: %s", file_str)
                error_count += 1
                continue

            if any(part in exclude for part in file_path.parts):
                skipped_count += 1
                continue

            root = file_path.parent
            result = await self._process_file(
                file_path,
                root,
                request,
                correlation_id,
            )
            if result is None:
                error_count += 1
                continue

            event, did_skip = result
            if did_skip:
                skipped_count += 1
                continue

            files_scanned += 1
            if event is not None:
                events.append(event)

        return files_scanned, skipped_count, error_count, truncated, events

    async def _process_file(
        self,
        file_path: Path,
        root: Path,
        request: ModelCrawlFilesystemRequest,
        correlation_id: UUID,
    ) -> tuple[ModelContentDiscoveredEvent | None, bool] | None:
        """Process a single file. Returns (event_or_none, was_skipped) or None on error."""
        try:
            stat = await asyncio.to_thread(file_path.stat)
        except OSError as exc:
            logger.warning("Cannot stat %s: %s", file_path, exc)
            return None

        if stat.st_size > request.max_file_size_bytes:
            logger.debug(
                "File too large, skipping: %s (%d bytes)", file_path, stat.st_size
            )
            return None, True

        content_type = detect_content_type(file_path)

        if (
            request.extensions is not None
            and file_path.suffix.lower() not in request.extensions
        ):
            return None, True

        try:
            content = await asyncio.to_thread(file_path.read_bytes)
        except OSError as exc:
            logger.warning("Cannot read %s: %s", file_path, exc)
            return None

        fingerprint = _compute_sha256(content)
        now_utc = datetime.now(UTC)

        try:
            relative_path = str(file_path.relative_to(root))
        except ValueError:
            relative_path = file_path.name

        event = ModelContentDiscoveredEvent(
            event_id=uuid4(),
            correlation_id=correlation_id,
            emitted_at_utc=now_utc,
            source_ref=str(file_path),
            content_type=content_type,
            content_fingerprint=fingerprint,
            file_size_bytes=stat.st_size,
            mtime=stat.st_mtime,
            root_path=str(root),
            relative_path=relative_path,
        )

        return event, False
