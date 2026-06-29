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
import os
import uuid
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

import yaml
from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnimemory.models.crawl.model_document_discovered_event import (
    ModelDocumentDiscoveredEvent,
)
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
from omnimarket.nodes.node_kreuzberg_parse_effect.models.model_kreuzberg_parse_config import (
    ModelKreuzbergParseConfig,
)
from omnimarket.nodes.node_kreuzberg_parse_effect.models.model_kreuzberg_parse_result import (
    ModelKreuzbergParseResult,
)

if TYPE_CHECKING:
    from omnimemory.models.crawl.model_document_changed_event import (
        ModelDocumentChangedEvent,
    )

_log = logging.getLogger(__name__)

_CONTRACT_PATH = Path(__file__).resolve().parent.parent / "contract.yaml"


def _resolve_config_from_contract() -> ModelKreuzbergParseConfig:
    """Resolve ``ModelKreuzbergParseConfig`` from the contract's config_fields.

    The contract ``config_fields`` block declares each field's env-var name and
    optional default. Endpoint and path values are read from those env vars per
    routing/config doctrine — never hardcoded in source. Fields with a contract
    default fall back to it when the env var is unset. This runs lazily at the
    handler boundary (inside ``handle``), so handler construction stays pure and
    resolver-satisfiable.
    """
    raw = yaml.safe_load(_CONTRACT_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{_CONTRACT_PATH} must contain a mapping")
    fields = raw.get("config_fields")
    if not isinstance(fields, dict):
        raise ValueError(f"{_CONTRACT_PATH} missing config_fields mapping")

    def _resolve(field_name: str) -> str | None:
        spec = fields.get(field_name)
        default = spec.get("default") if isinstance(spec, dict) else None
        value = os.environ.get(field_name)
        if value is not None:
            return value
        return str(default) if default is not None else None

    kreuzberg_url = _resolve("KREUZBERG_URL")
    text_store_path = _resolve("KREUZBERG_TEXT_STORE_PATH")
    parser_version = _resolve("KREUZBERG_PARSER_VERSION")
    document_root = _resolve("KREUZBERG_DOCUMENT_ROOT")
    missing = [
        name
        for name, val in (
            ("KREUZBERG_URL", kreuzberg_url),
            ("KREUZBERG_TEXT_STORE_PATH", text_store_path),
            ("KREUZBERG_PARSER_VERSION", parser_version),
            ("KREUZBERG_DOCUMENT_ROOT", document_root),
        )
        if val is None
    ]
    if missing:
        raise ValueError(
            "kreuzberg config could not be resolved from contract/env; "
            f"missing required values: {missing}"
        )

    overrides: dict[str, int] = {}
    for field_name, attr in (
        ("KREUZBERG_MAX_DOC_BYTES", "max_doc_bytes"),
        ("KREUZBERG_TIMEOUT_MS", "timeout_ms"),
        ("KREUZBERG_INLINE_TEXT_MAX_CHARS", "inline_text_max_chars"),
    ):
        resolved = _resolve(field_name)
        if resolved is not None:
            overrides[attr] = int(resolved)

    return ModelKreuzbergParseConfig(
        kreuzberg_url=kreuzberg_url,
        text_store_path=text_store_path,
        document_root=document_root,
        parser_version=parser_version,
        **overrides,
    )


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

    def __init__(self, config: ModelKreuzbergParseConfig | None = None) -> None:
        # Construction is pure and resolver-satisfiable: when ``config`` is not
        # injected (the runtime auto-wiring boot path constructs handlers with
        # zero args), it is resolved lazily from the contract on first use via
        # ``_ensure_config``. Tests inject ``config`` directly, short-circuiting
        # the lazy path. Resolving config in ``__init__`` here would force the
        # contract/env read into the boot constructor and crash the effects lane.
        self._config: ModelKreuzbergParseConfig | None = config
        self._config_lock = asyncio.Lock()

    async def _ensure_config(self) -> ModelKreuzbergParseConfig:
        """Return the parse config, resolving it from the contract once on demand.

        Single-flight under ``_config_lock``: concurrent ``handle`` calls on one
        event loop resolve EXACTLY ONE config. An injected config short-circuits
        without taking the lock.
        """
        if self._config is not None:
            return self._config
        async with self._config_lock:
            if self._config is None:
                self._config = await asyncio.to_thread(_resolve_config_from_contract)
            return self._config

    async def handle(
        self, envelope: ModelEventEnvelope[Any]
    ) -> ModelHandlerOutput[None]:
        """Canonical dispatch entrypoint for the effects runtime.

        Unwraps the document-discovered/changed event from the envelope payload,
        resolves config lazily, runs the real parse logic in ``process_event``,
        and returns the published facts as EFFECT events. This drives the
        same fully-wired kreuzberg-call/cache/emit path the consumer uses.
        """
        # Resolve config eagerly at the boundary so a misconfigured contract/env
        # fails fast here rather than mid-parse. ``process_event`` re-resolves
        # via the same single-flight cache (no duplicate construction).
        await self._ensure_config()
        payload = envelope.payload
        if isinstance(payload, (ModelDocumentDiscoveredEvent,)):
            event: ModelDocumentDiscoveredEvent | ModelDocumentChangedEvent = payload
        elif isinstance(payload, dict):
            event = ModelDocumentDiscoveredEvent.model_validate(payload)
        else:
            event = ModelDocumentDiscoveredEvent.model_validate(
                payload.model_dump()
                if hasattr(payload, "model_dump")
                else dict(payload)
            )

        emitted: list[ModelEventEnvelope[Any]] = []

        async def _capture(topic: str, message: dict[str, object]) -> None:
            emitted.append(
                ModelEventEnvelope(
                    payload=message,
                    correlation_id=envelope.correlation_id,
                    event_type=topic,
                )
            )

        await self.process_event(event=event, publish_callback=_capture)

        return ModelHandlerOutput.for_effect(
            input_envelope_id=envelope.envelope_id,
            correlation_id=envelope.correlation_id or uuid.uuid4(),
            handler_id="kreuzberg-parse-effect",
            events=tuple(emitted),
        )

    async def process_event(
        self,
        event: ModelDocumentDiscoveredEvent | ModelDocumentChangedEvent,
        publish_callback: Callable[[str, dict[str, object]], Coroutine[Any, Any, None]],
    ) -> ModelKreuzbergParseResult:
        """Process a single document discovered or changed event."""
        source_url = event.source_ref
        content_hash = event.content_fingerprint
        config = await self._ensure_config()

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
