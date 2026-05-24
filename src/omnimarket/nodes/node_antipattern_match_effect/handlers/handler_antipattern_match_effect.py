# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Handler for antipattern vector similarity search.

Embeds the query (code_text and/or description) via a contract-configured
embedding endpoint and searches the onex_antipatterns Qdrant collection for
nearest-neighbor antipattern entries.

Design invariants:
  - Embedding endpoint URL from EMBEDDING_MODEL_URL env var — fail-fast if unset.
  - Qdrant collection from ANTIPATTERN_QDRANT_COLLECTION (default: onex_antipatterns).
  - Qdrant unavailability is a graceful skip — returns empty matches + error_message.
  - Freshness decay: recently-discovered antipatterns ranked higher via configurable factor.
  - correlation_id required on both input and output.

[OMN-11919, OMN-11909]
"""

from __future__ import annotations

import logging
import math
import os
from datetime import UTC, datetime
from typing import Any

import httpx

from omnimarket.nodes.node_antipattern_match_effect.models.model_antipattern_match import (
    ModelAntipatternMatch,
)
from omnimarket.nodes.node_antipattern_match_effect.models.model_antipattern_match_response import (
    ModelAntipatternMatchResponse,
)

logger = logging.getLogger(__name__)

DEFAULT_QDRANT_COLLECTION = "onex_antipatterns"
DEFAULT_MIN_SIMILARITY = 0.75
DEFAULT_MAX_RESULTS = 5


def _build_query_text(
    code_text: str | None,
    description: str | None,
) -> str:
    """Combine code_text and/or description into a single embedding input."""
    normalized_description = (description or "").strip()
    normalized_code_text = (code_text or "").strip()
    if not normalized_description and not normalized_code_text:
        raise ValueError("At least one of code_text or description must be provided.")
    parts = []
    if normalized_description:
        parts.append(normalized_description)
    if normalized_code_text:
        parts.append(normalized_code_text)
    return "\n".join(parts)


def _apply_freshness_boost(
    base_score: float,
    discovered_at: str | None,
    decay_factor: float,
) -> float:
    """Apply time-based freshness boost to a similarity score.

    Boost = decay_factor * exp(-days_old / 365). Entries with no discovered_at
    receive no boost (treated as old).
    """
    if decay_factor == 0.0 or discovered_at is None:
        return base_score

    try:
        dt = datetime.fromisoformat(discovered_at.replace("Z", "+00:00"))
        now = datetime.now(tz=UTC)
        days_old = max((now - dt).days, 0)
        boost = decay_factor * math.exp(-days_old / 365.0)
        return base_score + boost
    except (ValueError, TypeError):
        return base_score


def _build_explanation(
    query_text: str,
    antipattern_name: str,
    description: str,
) -> str:
    """Build a human-readable explanation for why the query matched this antipattern."""
    return f"Query matched antipattern '{antipattern_name}': {description}"


class HandlerAntipatternMatchEffect:
    """EFFECT handler — embeds query and searches Qdrant for antipattern matches."""

    async def handle(
        self,
        *,
        correlation_id: str,
        code_text: str | None = None,
        description: str | None = None,
        min_similarity: float = DEFAULT_MIN_SIMILARITY,
        max_results: int = DEFAULT_MAX_RESULTS,
        freshness_decay_factor: float = 0.05,
        qdrant_client: Any | None = None,
        embedding_endpoint_override: str | None = None,
        qdrant_collection_override: str | None = None,
    ) -> ModelAntipatternMatchResponse:
        """Embed query and return matching antipatterns from vector store."""
        query_text = _build_query_text(code_text=code_text, description=description)

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

        resolved_client = qdrant_client
        if resolved_client is None:
            resolved_client = _build_qdrant_client()
            if resolved_client is None:
                logger.warning(
                    "Qdrant unavailable — skipping antipattern match (correlation_id=%s)",
                    correlation_id,
                )
                return ModelAntipatternMatchResponse(
                    correlation_id=correlation_id,
                    matches=(),
                    query_text_used=query_text,
                    qdrant_collection=collection,
                    error_message="Qdrant unavailable — no QDRANT_HOST configured.",
                )

        embedding = await _get_embedding(endpoint, query_text)
        if embedding is None:
            logger.warning(
                "Embedding generation failed (correlation_id=%s)", correlation_id
            )
            return ModelAntipatternMatchResponse(
                correlation_id=correlation_id,
                matches=(),
                query_text_used=query_text,
                qdrant_collection=collection,
                error_message="Embedding generation failed — check EMBEDDING_MODEL_URL.",
            )

        try:
            hits = resolved_client.search(
                collection_name=collection,
                query_vector=embedding,
                limit=max_results,
                with_payload=True,
            )
        except Exception:
            logger.exception("Qdrant search failed (correlation_id=%s)", correlation_id)
            return ModelAntipatternMatchResponse(
                correlation_id=correlation_id,
                matches=(),
                query_text_used=query_text,
                qdrant_collection=collection,
                error_message="Qdrant search failed.",
            )

        total_candidates = len(hits)

        matches: list[ModelAntipatternMatch] = []
        for hit in hits:
            score: float = hit.score
            if score < min_similarity:
                continue

            payload: dict[str, Any] = hit.payload or {}
            discovered_at: str | None = payload.get("discovered_at")
            adjusted = _apply_freshness_boost(
                base_score=score,
                discovered_at=discovered_at,
                decay_factor=freshness_decay_factor,
            )
            ap_name = str(payload.get("name") or "unknown").strip() or "unknown"
            ap_description = str(payload.get("description") or "").strip()
            matches.append(
                ModelAntipatternMatch(
                    similarity_score=score,
                    adjusted_score=adjusted,
                    antipattern_name=ap_name,
                    severity=payload.get("severity", ""),
                    enforcement=payload.get("enforcement", ""),
                    category=payload.get("category", ""),
                    description=ap_description,
                    rationale=payload.get("rationale", ""),
                    explanation=_build_explanation(
                        query_text=query_text,
                        antipattern_name=ap_name,
                        description=ap_description,
                    ),
                    source_ticket=payload.get("source_ticket", ""),
                    registry_version=payload.get("registry_version", ""),
                )
            )

        matches.sort(key=lambda m: m.adjusted_score, reverse=True)
        matches = matches[:max_results]

        logger.info(
            "Antipattern match: %d/%d candidates above threshold %.2f (correlation_id=%s)",
            len(matches),
            total_candidates,
            min_similarity,
            correlation_id,
        )

        return ModelAntipatternMatchResponse(
            correlation_id=correlation_id,
            matches=tuple(matches),
            query_text_used=query_text,
            qdrant_collection=collection,
            total_candidates_searched=total_candidates,
        )


def _build_qdrant_client() -> Any | None:
    """Build QdrantClient from env vars; returns None if unavailable."""
    try:
        from qdrant_client import QdrantClient
    except ImportError:
        logger.info("qdrant-client not installed; antipattern match will be a no-op")
        return None

    host = os.environ.get("QDRANT_HOST")  # contract-config-ok: config
    if not host:
        logger.info("QDRANT_HOST not set; antipattern match will be a no-op")
        return None

    raw_port = os.environ.get("QDRANT_PORT", "6333")  # contract-config-ok: config
    try:
        port = int(raw_port)
    except (TypeError, ValueError):
        logger.warning(
            "Invalid QDRANT_PORT=%r; antipattern match will be a no-op", raw_port
        )
        return None
    try:
        client = QdrantClient(host=host, port=port)
        client.get_collections()
        return client
    except Exception:
        logger.warning("Qdrant connection failed at %s:%d", host, port)
        return None


async def _get_embedding(endpoint: str, text: str) -> list[float] | None:
    """Call embedding endpoint; returns None on any failure."""
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{endpoint}/v1/embeddings",
                json={"input": text, "model": "embedding"},
            )
            response.raise_for_status()
            data = response.json()
            embedding: list[float] = data["data"][0]["embedding"]
            return embedding
    except (httpx.HTTPError, KeyError, IndexError, Exception):
        logger.warning("Embedding generation failed for endpoint=%r", endpoint)
        return None


__all__ = [
    "HandlerAntipatternMatchEffect",
    "_apply_freshness_boost",
    "_build_explanation",
    "_build_query_text",
]
