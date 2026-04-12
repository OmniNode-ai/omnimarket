# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2025 OmniNode.ai Inc.
"""Golden chain tests for HandlerKreuzbergParse (OMN-8299 Wave 3).

Covers:
- Parse success: mock kreuzberg returns text, document-indexed emitted
- too_large: file > max_doc_bytes emits parse-failed with error_code=too_large
- timeout: KreuzbergTimeoutError emits parse-failed with error_code=timeout
- HTTP error: KreuzbergExtractionError emits parse-failed with error_code=parse_error
- Idempotency: cached text file with matching fingerprint skips kreuzberg call
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Coroutine
from datetime import UTC
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from omnimarket.nodes.node_kreuzberg_parse_effect.clients.client_kreuzberg import (
    KreuzbergExtractionError,
    KreuzbergExtractResult,
    KreuzbergTimeoutError,
)
from omnimarket.nodes.node_kreuzberg_parse_effect.handlers.handler_kreuzberg_parse import (
    HandlerKreuzbergParse,
)
from omnimarket.nodes.node_kreuzberg_parse_effect.models.model_kreuzberg_parse_config import (
    ModelKreuzbergParseConfig,
)

type PublishRecord = tuple[str, dict[str, object]]


def make_config(
    tmp_path: Path, max_doc_bytes: int = 50_000_000
) -> ModelKreuzbergParseConfig:
    return ModelKreuzbergParseConfig(
        kreuzberg_url="http://localhost:8090",
        text_store_path=str(tmp_path / "text_store"),
        document_root=str(tmp_path),
        parser_version="1.0.0",
        max_doc_bytes=max_doc_bytes,
    )


def make_publish_capture() -> tuple[
    Callable[[str, dict[str, object]], Coroutine[Any, Any, None]],
    list[PublishRecord],
]:
    published: list[PublishRecord] = []

    async def capture(topic: str, payload: dict[str, object]) -> None:
        published.append((topic, payload))

    return capture, published


def make_discovered_event(source_ref: str, fingerprint: str) -> Any:
    from datetime import datetime

    from omnimemory.enums.crawl.enum_context_source_type import EnumContextSourceType
    from omnimemory.enums.crawl.enum_crawler_type import EnumCrawlerType
    from omnimemory.enums.crawl.enum_detected_doc_type import EnumDetectedDocType
    from omnimemory.models.crawl.model_document_discovered_event import (
        ModelDocumentDiscoveredEvent,
    )

    return ModelDocumentDiscoveredEvent(
        correlation_id=uuid4(),
        emitted_at_utc=datetime.now(UTC),
        crawler_type=EnumCrawlerType.FILESYSTEM,
        crawl_scope="test/scope",
        trigger_source="scheduled",
        source_ref=source_ref,
        source_type=EnumContextSourceType.REPO_DERIVED,
        source_version=None,
        content_fingerprint=fingerprint,
        content_blob_ref=f"sha256:{fingerprint}",
        token_estimate=10,
        scope_ref="omninode/shared",
        detected_doc_type=EnumDetectedDocType.UNKNOWN_MD,
        tags=[],
        priority_hint=35,
    )


class TestParseSuccess:
    @pytest.mark.unit
    async def test_parse_success_emits_indexed(self, tmp_path: Path) -> None:
        content = b"# Hello World"
        fingerprint = hashlib.sha256(content).hexdigest()
        md_file = tmp_path / "doc.md"
        md_file.write_bytes(content)

        config = make_config(tmp_path)
        handler = HandlerKreuzbergParse(config=config)
        publish_cb, published = make_publish_capture()
        event = make_discovered_event(str(md_file), fingerprint)

        with patch(
            "omnimarket.nodes.node_kreuzberg_parse_effect.handlers.handler_kreuzberg_parse.call_kreuzberg_extract",
            new=AsyncMock(
                return_value=KreuzbergExtractResult(extracted_text="Hello World")
            ),
        ):
            result = await handler.process_event(
                event=event, publish_callback=publish_cb
            )

        assert result.indexed_count == 1
        assert result.failed_count == 0
        topics = [t for t, _ in published]
        assert "onex.evt.omnimemory.document-indexed.v1" in topics

    @pytest.mark.unit
    async def test_parse_success_inline_text_in_event(self, tmp_path: Path) -> None:
        content = b"short"
        fingerprint = hashlib.sha256(content).hexdigest()
        md_file = tmp_path / "short.md"
        md_file.write_bytes(content)

        config = make_config(tmp_path)
        handler = HandlerKreuzbergParse(config=config)
        publish_cb, published = make_publish_capture()
        event = make_discovered_event(str(md_file), fingerprint)

        with patch(
            "omnimarket.nodes.node_kreuzberg_parse_effect.handlers.handler_kreuzberg_parse.call_kreuzberg_extract",
            new=AsyncMock(
                return_value=KreuzbergExtractResult(extracted_text="short text")
            ),
        ):
            result = await handler.process_event(
                event=event, publish_callback=publish_cb
            )

        assert result.indexed_count == 1
        _, payload = published[0]
        assert payload["extracted_text_ref"] == "short text"


class TestTooLarge:
    @pytest.mark.unit
    async def test_too_large_emits_parse_failed(self, tmp_path: Path) -> None:
        content = b"x" * 200
        fingerprint = hashlib.sha256(content).hexdigest()
        md_file = tmp_path / "big.md"
        md_file.write_bytes(content)

        config = make_config(tmp_path, max_doc_bytes=100)
        handler = HandlerKreuzbergParse(config=config)
        publish_cb, published = make_publish_capture()
        event = make_discovered_event(str(md_file), fingerprint)

        result = await handler.process_event(event=event, publish_callback=publish_cb)

        assert result.skipped_too_large_count == 1
        assert result.indexed_count == 0
        topics = [t for t, _ in published]
        assert "onex.evt.omnimemory.document-parse-failed.v1" in topics
        _, payload = published[0]
        assert payload["error_code"] == "too_large"


class TestTimeout:
    @pytest.mark.unit
    async def test_timeout_emits_parse_failed(self, tmp_path: Path) -> None:
        content = b"content"
        fingerprint = hashlib.sha256(content).hexdigest()
        md_file = tmp_path / "doc.md"
        md_file.write_bytes(content)

        config = make_config(tmp_path)
        handler = HandlerKreuzbergParse(config=config)
        publish_cb, published = make_publish_capture()
        event = make_discovered_event(str(md_file), fingerprint)

        with patch(
            "omnimarket.nodes.node_kreuzberg_parse_effect.handlers.handler_kreuzberg_parse.call_kreuzberg_extract",
            new=AsyncMock(side_effect=KreuzbergTimeoutError("timed out")),
        ):
            result = await handler.process_event(
                event=event, publish_callback=publish_cb
            )

        assert result.timeout_count == 1
        assert result.failed_count == 1
        _, payload = published[0]
        assert payload["error_code"] == "timeout"


class TestHttpError:
    @pytest.mark.unit
    async def test_http_error_emits_parse_failed(self, tmp_path: Path) -> None:
        content = b"content"
        fingerprint = hashlib.sha256(content).hexdigest()
        md_file = tmp_path / "doc.md"
        md_file.write_bytes(content)

        config = make_config(tmp_path)
        handler = HandlerKreuzbergParse(config=config)
        publish_cb, published = make_publish_capture()
        event = make_discovered_event(str(md_file), fingerprint)

        with patch(
            "omnimarket.nodes.node_kreuzberg_parse_effect.handlers.handler_kreuzberg_parse.call_kreuzberg_extract",
            new=AsyncMock(
                side_effect=KreuzbergExtractionError(
                    status_code=500, detail="internal error"
                )
            ),
        ):
            result = await handler.process_event(
                event=event, publish_callback=publish_cb
            )

        assert result.failed_count == 1
        assert result.indexed_count == 0
        _, payload = published[0]
        assert payload["error_code"] == "parse_error"


class TestIdempotency:
    @pytest.mark.unit
    async def test_cached_fingerprint_skips_kreuzberg(self, tmp_path: Path) -> None:
        content = b"cached content"
        fingerprint = hashlib.sha256(content).hexdigest()
        md_file = tmp_path / "doc.md"
        md_file.write_bytes(content)

        # Pre-populate cache
        text_store = tmp_path / "text_store"
        text_store.mkdir()
        slug = hashlib.sha256(str(md_file).encode()).hexdigest()
        cache_file = text_store / f"{slug}.txt"
        cache_file.write_text(
            f"fingerprint:{fingerprint}:1.0.0\nextracted text", encoding="utf-8"
        )

        config = make_config(tmp_path)
        handler = HandlerKreuzbergParse(config=config)
        publish_cb, published = make_publish_capture()
        event = make_discovered_event(str(md_file), fingerprint)

        with patch(
            "omnimarket.nodes.node_kreuzberg_parse_effect.handlers.handler_kreuzberg_parse.call_kreuzberg_extract",
            new=AsyncMock(side_effect=AssertionError("kreuzberg should not be called")),
        ):
            result = await handler.process_event(
                event=event, publish_callback=publish_cb
            )

        assert result.indexed_count == 1
        topics = [t for t, _ in published]
        assert "onex.evt.omnimemory.document-indexed.v1" in topics
