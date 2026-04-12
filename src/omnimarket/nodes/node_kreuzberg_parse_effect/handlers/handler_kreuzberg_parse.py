# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2025 OmniNode Team
"""KreuzbergParse handler: calls kreuzberg HTTP service to extract text from documents.

Migrated from omnimemory to omnimarket for OMN-8299 (Wave 3).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import mimetypes
import uuid
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from omnimemory.models.crawl.model_document_indexed_kreuzberg_event import (
    ModelDocumentIndexedKreuzbergEvent,
)
from omnimemory.models.crawl.model_document_parse_failed_event import (
    ModelDocumentParseFailedEvent,
)

from omnimarket.nodes.node_kreuzberg_parse_effect.clients.client_kreuzberg import (
    KreuzbergExtractionError,
    KreuzbergTimeoutError,
    call_kreuzberg_extract,
    read_cached_text,
    write_cached_text,
)
from omnimarket.nodes.node_kreuzberg_parse_effect.models.model_kreuzberg_parse_result import (
    ModelKreuzbergParseResult,
)

if TYPE_CHECKING:
    from omnimemory.models.crawl.model_document_changed_event import (
        ModelDocumentChangedEvent,
    )
    from omnimemory.models.crawl.model_document_discovered_event import (
        ModelDocumentDiscoveredEvent,
    )

    from omnimarket.nodes.node_kreuzberg_parse_effect.models.model_kreuzberg_parse_config import (
        ModelKreuzbergParseConfig,
    )

_log = logging.getLogger(__name__)


def _validate_source_path(source_url: str, document_root: Path) -> Path:
    candidate = Path(source_url)
    if not candidate.is_absolute():
        resolved = (document_root / candidate).resolve()
    else:
        resolved = candidate.resolve()

    try:
        resolved.relative_to(document_root.resolve())
    except ValueError as exc:
        raise ValueError(
            f"source_url {source_url!r} resolves to {resolved} which is outside "
            f"the permitted document root {document_root.resolve()}"
        ) from exc
    return resolved


def _compute_document_id(
    source_url: str,
    content_hash: str,
    parser_version: str,
) -> UUID:
    # Use NUL-delimited fields to avoid hash collisions from concatenation ambiguity.
    raw = "\x00".join([source_url, content_hash, parser_version]).encode("utf-8")
    digest = hashlib.sha256(raw).digest()
    return uuid.UUID(bytes=digest[:16])


def _cache_fingerprint(content_hash: str, parser_version: str) -> str:
    """Combine content hash and parser version so cache is invalidated on parser rollout."""
    return f"{content_hash}:{parser_version}"


def _source_url_slug(source_url: str) -> str:
    return hashlib.sha256(source_url.encode("utf-8")).hexdigest()


def _detect_mime_type(source_url: str) -> str:
    mime, _ = mimetypes.guess_type(source_url)
    return mime or "application/octet-stream"


class HandlerKreuzbergParse:
    """Handler that calls kreuzberg to extract text from documents."""

    def __init__(self, config: ModelKreuzbergParseConfig) -> None:
        self._config = config

    async def process_event(
        self,
        event: ModelDocumentDiscoveredEvent | ModelDocumentChangedEvent,
        publish_callback: Callable[[str, dict[str, object]], Coroutine[Any, Any, None]],
    ) -> ModelKreuzbergParseResult:
        """Process a single document discovered or changed event."""
        source_url = event.source_ref
        content_hash = event.content_fingerprint
        config = self._config

        indexed_topic = config.publish_topic_indexed
        failed_topic = config.publish_topic_parse_failed
        now = datetime.now(tz=UTC)

        document_root = Path(config.document_root)
        try:
            validated_path = _validate_source_path(source_url, document_root)
        except ValueError as exc:
            _log.warning(
                "Invalid source_ref path rejected",
                extra={"source_url": source_url, "error": str(exc)},
            )
            failed_event = ModelDocumentParseFailedEvent(
                correlation_id=event.correlation_id,
                emitted_at_utc=now,
                source_url=source_url,
                content_hash=content_hash,
                error_code="parse_error",
                error_detail=f"Invalid source_ref path: {exc}",
                parser_version=config.parser_version,
            )
            await publish_callback(failed_topic, failed_event.model_dump(mode="json"))
            return ModelKreuzbergParseResult(
                indexed_count=0,
                failed_count=1,
                skipped_too_large_count=0,
                timeout_count=0,
            )

        # Use the canonicalized path for all identity derivation so that relative
        # and absolute spellings of the same file produce the same document_id/cache key.
        canonical_source_url = str(validated_path)
        slug = _source_url_slug(canonical_source_url)
        text_store = Path(config.text_store_path)
        text_path = text_store / f"{slug}.txt"

        expected_fingerprint = _cache_fingerprint(content_hash, config.parser_version)
        cached = await asyncio.to_thread(read_cached_text, text_path)
        if cached is not None:
            stored_fingerprint, cached_text = cached
            if stored_fingerprint == expected_fingerprint:
                _log.debug(
                    "Idempotent: re-emitting indexed event without re-parsing",
                    extra={"source_url": source_url},
                )
                document_id = _compute_document_id(
                    canonical_source_url, content_hash, config.parser_version
                )
                extracted_text_ref = (
                    cached_text
                    if len(cached_text) < config.inline_text_max_chars
                    else f"file://{text_path.resolve()}"
                )
                indexed_event = ModelDocumentIndexedKreuzbergEvent(
                    correlation_id=event.correlation_id,
                    emitted_at_utc=now,
                    document_id=document_id,
                    source_url=source_url,
                    content_hash=content_hash,
                    extracted_text_ref=extracted_text_ref,
                    mime_type=_detect_mime_type(source_url),
                    parser_version=config.parser_version,
                )
                await publish_callback(
                    indexed_topic, indexed_event.model_dump(mode="json")
                )
                return ModelKreuzbergParseResult(
                    indexed_count=1,
                    failed_count=0,
                    skipped_too_large_count=0,
                    timeout_count=0,
                )

        try:
            file_size = await asyncio.to_thread(lambda: validated_path.stat().st_size)
        except OSError as exc:
            _log.warning(
                "Failed to stat source file",
                extra={"source_url": source_url, "error": str(exc)},
            )
            failed_event = ModelDocumentParseFailedEvent(
                correlation_id=event.correlation_id,
                emitted_at_utc=now,
                source_url=source_url,
                content_hash=content_hash,
                error_code="parse_error",
                error_detail=f"Failed to stat source file: {exc}",
                parser_version=config.parser_version,
            )
            await publish_callback(failed_topic, failed_event.model_dump(mode="json"))
            return ModelKreuzbergParseResult(
                indexed_count=0,
                failed_count=1,
                skipped_too_large_count=0,
                timeout_count=0,
            )

        if file_size > config.max_doc_bytes:
            _log.warning(
                "Document too large for kreuzberg",
                extra={
                    "source_url": source_url,
                    "size_bytes": file_size,
                    "max_doc_bytes": config.max_doc_bytes,
                },
            )
            failed_event = ModelDocumentParseFailedEvent(
                correlation_id=event.correlation_id,
                emitted_at_utc=now,
                source_url=source_url,
                content_hash=content_hash,
                error_code="too_large",
                error_detail=(
                    f"Document size {file_size} bytes exceeds "
                    f"max_doc_bytes={config.max_doc_bytes}"
                ),
                parser_version=config.parser_version,
            )
            await publish_callback(failed_topic, failed_event.model_dump(mode="json"))
            return ModelKreuzbergParseResult(
                indexed_count=0,
                failed_count=0,
                skipped_too_large_count=1,
                timeout_count=0,
            )

        try:
            file_bytes: bytes = await asyncio.to_thread(validated_path.read_bytes)
        except OSError as exc:
            _log.warning(
                "Failed to read source file",
                extra={"source_url": source_url, "error": str(exc)},
            )
            failed_event = ModelDocumentParseFailedEvent(
                correlation_id=event.correlation_id,
                emitted_at_utc=now,
                source_url=source_url,
                content_hash=content_hash,
                error_code="parse_error",
                error_detail=f"Failed to read source file: {exc}",
                parser_version=config.parser_version,
            )
            await publish_callback(failed_topic, failed_event.model_dump(mode="json"))
            return ModelKreuzbergParseResult(
                indexed_count=0,
                failed_count=1,
                skipped_too_large_count=0,
                timeout_count=0,
            )

        filename = validated_path.name
        mime_type = _detect_mime_type(source_url)
        timeout_seconds = config.timeout_ms / 1000.0

        try:
            result = await call_kreuzberg_extract(
                kreuzberg_url=config.kreuzberg_url,
                file_bytes=file_bytes,
                filename=filename,
                mime_type=mime_type,
                timeout_seconds=timeout_seconds,
            )
        except KreuzbergTimeoutError:
            _log.warning(
                "kreuzberg request timed out",
                extra={"source_url": source_url, "timeout_ms": config.timeout_ms},
            )
            failed_event = ModelDocumentParseFailedEvent(
                correlation_id=event.correlation_id,
                emitted_at_utc=now,
                source_url=source_url,
                content_hash=content_hash,
                error_code="timeout",
                error_detail=(
                    f"kreuzberg extract request timed out after {config.timeout_ms} ms"
                ),
                parser_version=config.parser_version,
            )
            await publish_callback(failed_topic, failed_event.model_dump(mode="json"))
            return ModelKreuzbergParseResult(
                indexed_count=0,
                failed_count=1,
                skipped_too_large_count=0,
                timeout_count=1,
            )
        except KreuzbergExtractionError as exc:
            _log.warning(
                "kreuzberg HTTP error",
                extra={"source_url": source_url, "status_code": exc.status_code},
            )
            failed_event = ModelDocumentParseFailedEvent(
                correlation_id=event.correlation_id,
                emitted_at_utc=now,
                source_url=source_url,
                content_hash=content_hash,
                error_code="parse_error",
                error_detail=exc.detail,
                parser_version=config.parser_version,
            )
            await publish_callback(failed_topic, failed_event.model_dump(mode="json"))
            return ModelKreuzbergParseResult(
                indexed_count=0,
                failed_count=1,
                skipped_too_large_count=0,
                timeout_count=0,
            )

        extracted_text = result.extracted_text

        try:
            await asyncio.to_thread(
                write_cached_text, text_path, expected_fingerprint, extracted_text
            )

            if len(extracted_text) < config.inline_text_max_chars:
                extracted_text_ref = extracted_text
            else:
                extracted_text_ref = f"file://{text_path.resolve()}"
        except OSError as exc:
            _log.warning(
                "Failed to write kreuzberg text cache; using inline fallback",
                extra={
                    "source_url": source_url,
                    "text_path": str(text_path),
                    "error": str(exc),
                },
            )
            if len(extracted_text) < config.inline_text_max_chars:
                extracted_text_ref = extracted_text
            else:
                failed_event = ModelDocumentParseFailedEvent(
                    correlation_id=event.correlation_id,
                    emitted_at_utc=now,
                    source_url=source_url,
                    content_hash=content_hash,
                    error_code="parse_error",
                    error_detail="cache write failed and text too large to inline",
                    parser_version=config.parser_version,
                )
                await publish_callback(
                    failed_topic, failed_event.model_dump(mode="json")
                )
                return ModelKreuzbergParseResult(
                    indexed_count=0,
                    failed_count=1,
                    skipped_too_large_count=0,
                    timeout_count=0,
                )

        document_id = _compute_document_id(
            canonical_source_url, content_hash, config.parser_version
        )
        indexed_event = ModelDocumentIndexedKreuzbergEvent(
            correlation_id=event.correlation_id,
            emitted_at_utc=now,
            document_id=document_id,
            source_url=source_url,
            content_hash=content_hash,
            extracted_text_ref=extracted_text_ref,
            mime_type=mime_type,
            parser_version=config.parser_version,
        )
        await publish_callback(indexed_topic, indexed_event.model_dump(mode="json"))
        _log.info(
            "kreuzberg parse complete",
            extra={
                "source_url": source_url,
                "document_id": str(document_id),
                "extracted_text_len": len(extracted_text),
            },
        )
        return ModelKreuzbergParseResult(
            indexed_count=1,
            failed_count=0,
            skipped_too_large_count=0,
            timeout_count=0,
        )
