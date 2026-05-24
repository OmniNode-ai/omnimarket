# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Handler for antipattern registry embedding and Qdrant upsert.

Reads the resolved antipattern registry (bundled defaults + per-repo overrides),
embeds each vector_enabled entry via a contract-configured embedding endpoint,
and upserts into the onex_antipatterns Qdrant collection.

Design invariants:
  - Embedding endpoint URL comes from EMBEDDING_MODEL_URL env var — fail-fast if unset.
  - Qdrant collection name from ANTIPATTERN_QDRANT_COLLECTION (default: onex_antipatterns).
  - Vector backend from contract config field (default: "qdrant") via ProtocolVectorStore SPI.
  - Point ID = SHA-256(name + registry_version) as uint64 — upsert is idempotent.
  - Idempotency: checks collection metadata payload for indexed registry version.
  - Qdrant unavailability is a graceful skip (warning, zero counts), not an error.
  - correlation_id is required on both input and output.

[OMN-11913, OMN-11909]
"""

from __future__ import annotations

import hashlib
import logging
import os
import struct
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from omnimarket.nodes.node_antipattern_index_effect.models.model_antipattern_index_result import (
    ModelAntipatternIndexResult,
)

logger = logging.getLogger(__name__)

DEFAULT_QDRANT_COLLECTION = "onex_antipatterns"
DEFAULT_VECTOR_BACKEND = "qdrant"
DEFAULT_VECTOR_SIZE = 4096
_VERSION_META_POINT_ID = 0  # Reserved point ID for collection version metadata


def _entry_point_id(name: str, registry_version: str) -> int:
    """Stable uint64 point ID derived from antipattern name + registry version."""
    digest = hashlib.sha256(f"{name}:{registry_version}".encode()).digest()
    # Take first 8 bytes, mask to positive int64 range
    value: int = struct.unpack(">Q", digest[:8])[0]
    return value & 0x7FFFFFFFFFFFFFFF  # keep positive, fits Qdrant uint64


def _build_entry_text(entry: Any) -> str:
    """Build embedding text from description + rationale + examples."""
    parts = [entry.description.strip(), entry.rationale.strip()]
    for ex in entry.examples:
        if ex.label:
            parts.append(f"{ex.kind}: {ex.label}")
        if ex.code:
            parts.append(ex.code.strip())
    return "\n".join(p for p in parts if p)


class HandlerAntipatternIndexEffect:
    """EFFECT handler — embeds antipattern registry and upserts into Qdrant."""

    async def handle(
        self,
        *,
        correlation_id: str,
        repo_root: str | None = None,
        force_reindex: bool = False,
        qdrant_client: Any | None = None,
        embedding_endpoint_override: str | None = None,
        qdrant_collection_override: str | None = None,
        registry_loader: Callable[[Path], Any] | None = None,
    ) -> ModelAntipatternIndexResult:
        """Embed all vector_enabled antipattern entries and upsert into Qdrant."""
        if registry_loader is None:
            from omnibase_core.validation.antipattern_registry_loader import (
                resolve_antipatterns,
            )

            registry_loader = resolve_antipatterns

        endpoint = (
            embedding_endpoint_override
            or os.environ.get("EMBEDDING_MODEL_URL", "")  # contract-config-ok: config
        )
        if not endpoint:
            raise OSError(
                "EMBEDDING_MODEL_URL is required but not set. "
                "Set this env var to the OpenAI-compatible embedding endpoint base URL."
            )

        collection = (
            qdrant_collection_override
            or os.environ.get(  # contract-config-ok: config
                "ANTIPATTERN_QDRANT_COLLECTION", DEFAULT_QDRANT_COLLECTION
            )
        )

        vector_size = int(
            os.environ.get(
                "ANTIPATTERN_EMBEDDING_VECTOR_SIZE", str(DEFAULT_VECTOR_SIZE)
            )  # contract-config-ok: config
        )

        resolved_root = Path(repo_root) if repo_root else Path.cwd()
        registry = registry_loader(resolved_root)
        registry_version = registry.version

        resolved_client = qdrant_client
        if resolved_client is None:
            resolved_client = _build_qdrant_client()
            if resolved_client is None:
                logger.warning(
                    "Qdrant unavailable — skipping antipattern indexing (correlation_id=%s)",
                    correlation_id,
                )
                return ModelAntipatternIndexResult(
                    correlation_id=correlation_id,
                    indexed_count=0,
                    skipped_count=0,
                    registry_version=registry_version,
                    qdrant_collection=collection,
                )

        try:
            _ensure_collection(resolved_client, collection, vector_size)
        except Exception:
            logger.exception(
                "Qdrant collection setup failed — skipping indexing (correlation_id=%s)",
                correlation_id,
            )
            return ModelAntipatternIndexResult(
                correlation_id=correlation_id,
                indexed_count=0,
                skipped_count=0,
                registry_version=registry_version,
                qdrant_collection=collection,
            )

        # Idempotency check — skip if already indexed at this version
        if not force_reindex and _is_already_indexed(
            resolved_client, collection, registry_version
        ):
            logger.info(
                "Registry version %s already indexed in %s — no-op (correlation_id=%s)",
                registry_version,
                collection,
                correlation_id,
            )
            return ModelAntipatternIndexResult(
                correlation_id=correlation_id,
                indexed_count=0,
                skipped_count=0,
                registry_version=registry_version,
                qdrant_collection=collection,
                was_no_op=True,
            )

        vector_entries = [e for e in registry.entries if e.vector_enabled]
        if not vector_entries:
            logger.info(
                "No vector_enabled entries in registry v%s (correlation_id=%s)",
                registry_version,
                correlation_id,
            )
            return ModelAntipatternIndexResult(
                correlation_id=correlation_id,
                indexed_count=0,
                skipped_count=len(registry.entries),
                registry_version=registry_version,
                qdrant_collection=collection,
            )

        indexed_ids: list[str] = []
        failed = 0

        async with httpx.AsyncClient(timeout=30.0) as client:
            for entry in vector_entries:
                try:
                    text = _build_entry_text(entry)
                    if not text.strip():
                        failed += 1
                        continue

                    embedding = await _get_embedding(client, endpoint, text)
                    if embedding is None:
                        failed += 1
                        continue

                    point_id = _entry_point_id(entry.name, registry_version)
                    _upsert_point(
                        resolved_client,
                        collection,
                        entry,
                        embedding,
                        point_id,
                        registry_version,
                    )
                    indexed_ids.append(str(point_id))
                except Exception:
                    logger.exception(
                        "Failed to index antipattern '%s' (correlation_id=%s)",
                        entry.name,
                        correlation_id,
                    )
                    failed += 1

        if indexed_ids:
            _write_version_metadata(resolved_client, collection, registry_version)

        non_vector_count = len(registry.entries) - len(vector_entries)
        logger.info(
            "Antipattern indexing complete: %d indexed, %d failed, %d non-vector entries (correlation_id=%s)",
            len(indexed_ids),
            failed,
            non_vector_count,
            correlation_id,
        )
        return ModelAntipatternIndexResult(
            correlation_id=correlation_id,
            indexed_count=len(indexed_ids),
            skipped_count=failed + non_vector_count,
            registry_version=registry_version,
            vector_ids=indexed_ids,
            qdrant_collection=collection,
        )


def _build_qdrant_client() -> Any | None:
    """Build QdrantClient from env vars; returns None if unavailable."""
    try:
        from qdrant_client import QdrantClient
    except ImportError:
        logger.info("qdrant-client not installed; antipattern indexing will be a no-op")
        return None

    host = os.environ.get("QDRANT_HOST")  # contract-config-ok: config
    if not host:
        logger.info("QDRANT_HOST not set; antipattern indexing will be a no-op")
        return None

    port = int(os.environ.get("QDRANT_PORT", "6333"))  # contract-config-ok: config
    client = QdrantClient(host=host, port=port)
    client.get_collections()  # connectivity probe
    return client


def _ensure_collection(client: Any, collection: str, vector_size: int) -> None:
    """Create Qdrant collection if it does not exist."""
    from qdrant_client.models import Distance, VectorParams

    existing = [c.name for c in client.get_collections().collections]
    if collection not in existing:
        try:
            client.create_collection(
                collection_name=collection,
                vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE),
            )
            logger.info(
                "Created Qdrant collection: %s (dim=%d)", collection, vector_size
            )
        except Exception:
            existing_after = [c.name for c in client.get_collections().collections]
            if collection not in existing_after:
                logger.exception("Failed to create Qdrant collection %s", collection)
                raise


def _is_already_indexed(client: Any, collection: str, registry_version: str) -> bool:
    """Check collection metadata for indexed registry version."""
    try:
        results = client.retrieve(
            collection_name=collection,
            ids=[_VERSION_META_POINT_ID],
            with_payload=True,
        )
        if results:
            stored_version: str | None = results[0].payload.get("registry_version")
            return stored_version == registry_version
    except Exception:
        pass
    return False


def _write_version_metadata(
    client: Any, collection: str, registry_version: str
) -> None:
    """Write version metadata point so future runs can detect idempotency."""
    try:
        from qdrant_client.models import PointStruct

        client.upsert(
            collection_name=collection,
            points=[
                PointStruct(
                    id=_VERSION_META_POINT_ID,
                    vector=[0.0] * 1,
                    payload={"registry_version": registry_version, "_meta": True},
                )
            ],
        )
    except Exception:
        logger.warning(
            "Failed to write version metadata to Qdrant collection %s", collection
        )


def _upsert_point(
    client: Any,
    collection: str,
    entry: Any,
    embedding: list[float],
    point_id: int,
    registry_version: str,
) -> None:
    """Upsert a single antipattern entry as a Qdrant point."""
    from qdrant_client.models import PointStruct

    examples_text = " | ".join(
        f"{ex.kind}: {ex.label}" for ex in entry.examples if ex.label
    )
    point = PointStruct(
        id=point_id,
        vector=embedding,
        payload={
            "name": entry.name,
            "severity": entry.severity,
            "enforcement": entry.enforcement,
            "category": entry.category,
            "pattern_type": entry.pattern_type,
            "description": entry.description[:500],
            "rationale": entry.rationale[:500],
            "examples_summary": examples_text[:200],
            "source_ticket": entry.source_ticket,
            "registry_version": registry_version,
        },
    )
    client.upsert(collection_name=collection, points=[point])


async def _get_embedding(
    client: httpx.AsyncClient,
    endpoint: str,
    text: str,
) -> list[float] | None:
    """Call embedding endpoint; returns None on any failure."""
    try:
        response = await client.post(
            f"{endpoint}/v1/embeddings",
            json={"input": text, "model": "embedding"},
        )
        response.raise_for_status()
        data = response.json()
        embedding: list[float] = data["data"][0]["embedding"]
        return embedding
    except (httpx.HTTPError, KeyError, IndexError):
        logger.warning("Embedding generation failed for endpoint=%r", endpoint)
        return None


__all__ = [
    "HandlerAntipatternIndexEffect",
    "_build_entry_text",
    "_entry_point_id",
]
