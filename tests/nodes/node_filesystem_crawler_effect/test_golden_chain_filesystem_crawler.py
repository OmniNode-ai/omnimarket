# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2025 OmniNode Team
"""Golden chain tests for HandlerFilesystemCrawler (OMN-8299 Wave 3).

Covers:
- Document discovered: new .md file emits document-discovered + document-indexed
- Document changed: mtime changed + different SHA-256 emits document-changed + document-indexed
- Document removed: state table entry absent from walk emits document-removed
- mtime fast-path: unchanged mtime skips re-read, no events emitted
- mtime changed but content unchanged: updates state, no events emitted
- File size limit: files exceeding max_file_size_bytes are skipped
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Coroutine
from datetime import UTC
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from omnimemory.enums.crawl.enum_crawler_type import EnumCrawlerType
from omnimemory.models.crawl.model_crawl_state_record import ModelCrawlStateRecord

from omnimarket.nodes.node_filesystem_crawler_effect.handlers.handler_filesystem_crawler import (
    HandlerFilesystemCrawler,
)
from omnimarket.nodes.node_filesystem_crawler_effect.models.model_filesystem_crawler_config import (
    ModelFilesystemCrawlerConfig,
)

type PublishRecord = tuple[str, dict[str, object]]


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def make_config(
    path_prefixes: list[str],
    max_file_size_bytes: int = 5_242_880,
    max_files_per_crawl: int = 10_000,
) -> ModelFilesystemCrawlerConfig:
    return ModelFilesystemCrawlerConfig(
        path_prefixes=path_prefixes,
        max_file_size_bytes=max_file_size_bytes,
        max_files_per_crawl=max_files_per_crawl,
    )


def make_state(
    source_ref: str,
    fingerprint: str,
    scope_ref: str = "omninode/shared",
    mtime: float | None = None,
) -> ModelCrawlStateRecord:
    from datetime import datetime

    return ModelCrawlStateRecord(
        source_ref=source_ref,
        crawler_type=EnumCrawlerType.FILESYSTEM,
        scope_ref=scope_ref,
        content_fingerprint=fingerprint,
        source_version=None,
        last_crawled_at_utc=datetime.now(UTC),
        last_known_mtime=mtime,
    )


def make_mock_repo() -> MagicMock:
    repo = MagicMock()
    repo.get_state = AsyncMock(return_value=None)
    repo.list_states_for_scope = AsyncMock(return_value=[])
    repo.upsert_state = AsyncMock()
    repo.delete_state = AsyncMock()
    return repo


def make_publish_capture() -> tuple[
    Callable[[str, dict[str, object]], Coroutine[object, object, None]],
    list[PublishRecord],
]:
    published: list[PublishRecord] = []

    async def capture(topic: str, payload: dict[str, object]) -> None:
        published.append((topic, payload))

    return capture, published


class TestDocumentDiscovered:
    @pytest.mark.unit
    async def test_new_md_file_emits_discovered_and_indexed(
        self, tmp_path: Path
    ) -> None:
        md_file = tmp_path / "README.md"
        md_file.write_bytes(b"# Hello")

        repo = make_mock_repo()
        config = make_config(path_prefixes=[str(tmp_path)])
        handler = HandlerFilesystemCrawler(config=config, crawl_state_repo=repo)
        publish_cb, published = make_publish_capture()

        result = await handler.crawl(
            correlation_id=uuid4(),
            crawl_scope="test/scope",
            trigger_source="scheduled",
            publish_callback=publish_cb,
        )

        assert result.discovered_count == 1
        assert result.indexed_count == 1
        assert result.changed_count == 0
        topics = [t for t, _ in published]
        assert "onex.evt.omnimemory.document-discovered.v1" in topics
        assert "onex.evt.omnimemory.document-indexed.v1" in topics

    @pytest.mark.unit
    async def test_multiple_md_files_all_discovered(self, tmp_path: Path) -> None:
        for name in ("a.md", "b.md", "c.md"):
            (tmp_path / name).write_bytes(b"content")

        repo = make_mock_repo()
        config = make_config(path_prefixes=[str(tmp_path)])
        handler = HandlerFilesystemCrawler(config=config, crawl_state_repo=repo)
        publish_cb, _published = make_publish_capture()

        result = await handler.crawl(
            correlation_id=uuid4(),
            crawl_scope="test/scope",
            trigger_source="manual",
            publish_callback=publish_cb,
        )

        assert result.discovered_count == 3
        assert result.indexed_count == 3


class TestDocumentChanged:
    @pytest.mark.unit
    async def test_content_changed_emits_changed_and_indexed(
        self, tmp_path: Path
    ) -> None:
        md_file = tmp_path / "doc.md"
        new_content = b"updated content"
        md_file.write_bytes(new_content)

        old_fingerprint = _sha256(b"old content")
        new_fingerprint = _sha256(new_content)
        assert old_fingerprint != new_fingerprint

        prior_state = make_state(
            source_ref=str(md_file.resolve()),
            fingerprint=old_fingerprint,
            mtime=0.0,  # force mtime path (current mtime != 0.0)
        )
        repo = make_mock_repo()
        repo.get_state = AsyncMock(return_value=prior_state)

        config = make_config(path_prefixes=[str(tmp_path)])
        handler = HandlerFilesystemCrawler(config=config, crawl_state_repo=repo)
        publish_cb, published = make_publish_capture()

        result = await handler.crawl(
            correlation_id=uuid4(),
            crawl_scope="test/scope",
            trigger_source="scheduled",
            publish_callback=publish_cb,
        )

        assert result.changed_count == 1
        assert result.indexed_count == 1
        assert result.discovered_count == 0
        topics = [t for t, _ in published]
        assert "onex.evt.omnimemory.document-changed.v1" in topics
        assert "onex.evt.omnimemory.document-indexed.v1" in topics

    @pytest.mark.unit
    async def test_mtime_changed_content_unchanged_no_events(
        self, tmp_path: Path
    ) -> None:
        content = b"same content"
        md_file = tmp_path / "doc.md"
        md_file.write_bytes(content)

        fingerprint = _sha256(content)
        prior_state = make_state(
            source_ref=str(md_file.resolve()),
            fingerprint=fingerprint,
            mtime=0.0,  # different from actual mtime → forces hash check
        )
        repo = make_mock_repo()
        repo.get_state = AsyncMock(return_value=prior_state)

        config = make_config(path_prefixes=[str(tmp_path)])
        handler = HandlerFilesystemCrawler(config=config, crawl_state_repo=repo)
        publish_cb, published = make_publish_capture()

        result = await handler.crawl(
            correlation_id=uuid4(),
            crawl_scope="test/scope",
            trigger_source="scheduled",
            publish_callback=publish_cb,
        )

        assert result.unchanged_count == 1
        assert result.discovered_count == 0
        assert result.changed_count == 0
        assert len(published) == 0


class TestMtimeFastPath:
    @pytest.mark.unit
    async def test_unchanged_mtime_skips_read_no_events(self, tmp_path: Path) -> None:
        content = b"content"
        md_file = tmp_path / "doc.md"
        md_file.write_bytes(content)

        actual_mtime = md_file.stat().st_mtime
        fingerprint = _sha256(content)
        prior_state = make_state(
            source_ref=str(md_file.resolve()),
            fingerprint=fingerprint,
            mtime=actual_mtime,
        )
        repo = make_mock_repo()
        repo.get_state = AsyncMock(return_value=prior_state)

        config = make_config(path_prefixes=[str(tmp_path)])
        handler = HandlerFilesystemCrawler(config=config, crawl_state_repo=repo)
        publish_cb, published = make_publish_capture()

        result = await handler.crawl(
            correlation_id=uuid4(),
            crawl_scope="test/scope",
            trigger_source="scheduled",
            publish_callback=publish_cb,
        )

        assert result.mtime_skipped_count == 1
        assert result.discovered_count == 0
        assert result.changed_count == 0
        assert len(published) == 0
        # State upserted (last_crawled_at_utc update) but no events
        repo.upsert_state.assert_called_once()


class TestDocumentRemoved:
    @pytest.mark.unit
    async def test_missing_file_emits_removed(self, tmp_path: Path) -> None:
        stale_path = str(tmp_path / "gone.md")
        stale_state = make_state(source_ref=stale_path, fingerprint=_sha256(b"abc123"))

        repo = make_mock_repo()
        # No files in tmp_path, but stale state record exists
        repo.list_states_for_scope = AsyncMock(return_value=[stale_state])

        config = make_config(path_prefixes=[str(tmp_path)])
        handler = HandlerFilesystemCrawler(config=config, crawl_state_repo=repo)
        publish_cb, published = make_publish_capture()

        result = await handler.crawl(
            correlation_id=uuid4(),
            crawl_scope="test/scope",
            trigger_source="scheduled",
            publish_callback=publish_cb,
        )

        assert result.removed_count == 1
        topics = [t for t, _ in published]
        assert "onex.evt.omnimemory.document-removed.v1" in topics
        repo.delete_state.assert_called_once()


class TestFileSizeLimit:
    @pytest.mark.unit
    async def test_oversized_file_skipped(self, tmp_path: Path) -> None:
        md_file = tmp_path / "big.md"
        md_file.write_bytes(b"x" * 100)

        config = make_config(
            path_prefixes=[str(tmp_path)],
            max_file_size_bytes=50,  # 50 bytes limit
        )
        repo = make_mock_repo()
        handler = HandlerFilesystemCrawler(config=config, crawl_state_repo=repo)
        publish_cb, published = make_publish_capture()

        result = await handler.crawl(
            correlation_id=uuid4(),
            crawl_scope="test/scope",
            trigger_source="scheduled",
            publish_callback=publish_cb,
        )

        assert result.skipped_count == 1
        assert result.discovered_count == 0
        assert len(published) == 0
