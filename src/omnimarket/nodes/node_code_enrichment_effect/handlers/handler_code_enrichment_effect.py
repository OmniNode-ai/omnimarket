# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Handler for LLM-based code entity enrichment.

Re-implements omniintelligence dispatch_handler_code_enrichment (deleted in PR #568)
as a proper omnimarket EFFECT node handler with contract-driven model routing.

Design invariants (from recovered source + OMN-5664 contract):
  - Primary LLM endpoint from LLM_CODER_URL env var — fail-fast if unset.
  - Fallback to LLM_FALLBACK_URL if primary returns a connection error.
  - Low confidence (<0.7, configurable via CODE_ENRICHMENT_CONFIDENCE_THRESHOLD)
    → classification stored as "other" (Invariant S5 — don't force an archetype label).
  - LLM failure is non-fatal: entity stays unenriched, retried on next run.
  - enrichment_version from CODE_ENRICHMENT_VERSION env var (default "1.0.0").
  - No hardcoded IPs, no hardcoded model IDs, no hardcoded topic literals.
  - correlation_id required on input and output.

[OMN-5657, OMN-5664]
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Protocol, runtime_checkable

import httpx

from omnimarket.nodes.node_code_enrichment_effect.models.model_code_enrichment_result import (
    ModelCodeEnrichmentResult,
)

logger = logging.getLogger(__name__)

# Topic bindings from contract.yaml event_bus
TOPIC_CODE_ENTITIES_EXTRACTED: str = "onex.evt.omnimarket.code-entities-extracted.v1"
TOPIC_CODE_ENRICHED: str = "onex.evt.omnimarket.code-enriched.v1"

DEFAULT_ENRICHMENT_BATCH_SIZE = 25
DEFAULT_CONFIDENCE_THRESHOLD = 0.7
DEFAULT_ENRICHMENT_VERSION = "1.0.0"

ENRICHMENT_CLASSIFICATIONS: list[str] = [
    "factory",
    "handler",
    "adapter",
    "model",
    "protocol",
    "utility",
    "effect",
    "compute",
    "orchestrator",
    "reducer",
    "repository",
    "middleware",
]

PROMPT_TEMPLATE: str = (
    "Given this Python class:\n"
    "Name: {entity_name}\n"
    "Base classes: {bases}\n"
    "Methods: {methods}\n"
    "Docstring: {docstring}\n\n"
    "1. Classify it as one of: {classifications}\n"
    "2. Write a 1-sentence description of what it does.\n"
    "3. What architectural pattern does it follow?\n\n"
    'If none fit confidently, use "other".\n'
    'Respond as JSON: {{"classification": "...", "confidence": 0.0-1.0, "description": "...", "pattern": "..."}}'
)


@runtime_checkable
class ProtocolCodeEntityRepository(Protocol):
    async def get_entities_needing_enrichment(
        self, *, limit: int
    ) -> list[dict[str, Any]]: ...
    async def update_enrichment(
        self,
        *,
        entity_id: str,
        classification: str,
        llm_description: str,
        architectural_pattern: str,
        classification_confidence: float,
        enrichment_version: str,
    ) -> None: ...


class HandlerCodeEnrichmentEffect:
    """EFFECT handler — enriches code entities with LLM classification and description."""

    async def handle(
        self,
        *,
        correlation_id: str,
        repository: ProtocolCodeEntityRepository,
        llm_endpoint_override: str | None = None,
        batch_size: int | None = None,
    ) -> ModelCodeEnrichmentResult:
        """Enrich a batch of unenriched code entities with LLM classification."""
        primary_endpoint = llm_endpoint_override or os.environ.get("LLM_CODER_URL", "")
        if not primary_endpoint:
            raise OSError(
                "LLM_CODER_URL is required but not set. "
                "Set this env var to the primary Qwen3-Coder OpenAI-compatible endpoint base URL."
            )

        fallback_endpoint = os.environ.get("LLM_FALLBACK_URL", "")
        confidence_threshold = float(
            os.environ.get(
                "CODE_ENRICHMENT_CONFIDENCE_THRESHOLD",
                str(DEFAULT_CONFIDENCE_THRESHOLD),
            )
        )
        enrichment_version = os.environ.get(
            "CODE_ENRICHMENT_VERSION", DEFAULT_ENRICHMENT_VERSION
        )
        if batch_size is not None and batch_size <= 0:
            raise ValueError(f"batch_size must be a positive integer, got {batch_size}")
        effective_batch_size = (
            batch_size
            if batch_size is not None and batch_size > 0
            else int(
                os.environ.get(
                    "CODE_ENRICHMENT_BATCH_SIZE", str(DEFAULT_ENRICHMENT_BATCH_SIZE)
                )
            )
        )

        entities = await repository.get_entities_needing_enrichment(
            limit=effective_batch_size
        )
        if not entities:
            logger.info(
                "No entities needing enrichment (correlation_id=%s)", correlation_id
            )
            return ModelCodeEnrichmentResult(
                correlation_id=correlation_id,
                enriched_count=0,
                failed_count=0,
                batch_size_used=effective_batch_size,
                enrichment_version=enrichment_version,
            )

        enriched = 0
        failed = 0

        async with httpx.AsyncClient(timeout=30.0) as client:
            for entity in entities:
                try:
                    result = await _enrich_single_entity(
                        client=client,
                        primary_endpoint=primary_endpoint,
                        fallback_endpoint=fallback_endpoint,
                        entity=entity,
                    )
                    if result is not None:
                        raw_classification = result.get("classification", "other")
                        raw_confidence = result.get("confidence", 0.0)

                        try:
                            confidence = float(raw_confidence)
                        except (TypeError, ValueError):
                            logger.warning(
                                "LLM returned non-numeric confidence %r for %s — defaulting to 0.0",
                                raw_confidence,
                                entity.get("entity_name"),
                            )
                            confidence = 0.0

                        if (
                            raw_classification not in ENRICHMENT_CLASSIFICATIONS
                            or confidence < confidence_threshold
                        ):
                            classification = "other"
                        else:
                            classification = raw_classification

                        await repository.update_enrichment(
                            entity_id=str(entity["id"]),
                            classification=classification,
                            llm_description=result.get("description", ""),
                            architectural_pattern=result.get("pattern", ""),
                            classification_confidence=confidence,
                            enrichment_version=enrichment_version,
                        )
                        enriched += 1
                    else:
                        failed += 1
                except Exception:
                    logger.exception(
                        "Failed to enrich entity %s (correlation_id=%s)",
                        entity.get("entity_name"),
                        correlation_id,
                    )
                    failed += 1

        logger.info(
            "Enrichment complete: %d enriched, %d failed (correlation_id=%s)",
            enriched,
            failed,
            correlation_id,
        )
        return ModelCodeEnrichmentResult(
            correlation_id=correlation_id,
            enriched_count=enriched,
            failed_count=failed,
            batch_size_used=effective_batch_size,
            enrichment_version=enrichment_version,
        )


async def _enrich_single_entity(
    *,
    client: httpx.AsyncClient,
    primary_endpoint: str,
    fallback_endpoint: str,
    entity: dict[str, Any],
) -> dict[str, Any] | None:
    """Call LLM for a single entity, falling back to fallback_endpoint only on connection error."""
    prompt = PROMPT_TEMPLATE.format(
        entity_name=entity.get("entity_name", ""),
        bases=", ".join(entity.get("bases") or []),
        methods=", ".join(m.get("name", "") for m in (entity.get("methods") or [])),
        docstring=entity.get("docstring") or "(none)",
        classifications=", ".join(ENRICHMENT_CLASSIFICATIONS),
    )

    result, primary_connect_error = await _call_llm(
        client, primary_endpoint, prompt, entity.get("entity_name", "")
    )
    if result is not None:
        return result

    if (
        primary_connect_error
        and fallback_endpoint
        and fallback_endpoint != primary_endpoint
    ):
        logger.warning(
            "Primary LLM unreachable (%s), trying fallback for %s",
            primary_endpoint,
            entity.get("entity_name"),
        )
        fallback_result, _ = await _call_llm(
            client, fallback_endpoint, prompt, entity.get("entity_name", "")
        )
        return fallback_result

    return None


async def _call_llm(
    client: httpx.AsyncClient,
    endpoint: str,
    prompt: str,
    entity_name: str,
) -> tuple[dict[str, Any] | None, bool]:
    """Call the LLM endpoint and return (result, connect_error_flag).

    connect_error_flag is True only when the primary was unreachable (ConnectError),
    signalling that a fallback attempt is warranted. Non-connection errors (bad JSON,
    HTTP errors) return False so fallback is not triggered.
    """
    try:
        response = await client.post(
            f"{endpoint}/v1/chat/completions",
            json={
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 200,
                "temperature": 0.1,
            },
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"].strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        result: dict[str, Any] = json.loads(content)
        return result, False
    except httpx.ConnectError:
        logger.warning("LLM connection failed at %s for %s", endpoint, entity_name)
        return None, True
    except (httpx.HTTPError, json.JSONDecodeError, KeyError, IndexError):
        logger.warning("LLM enrichment failed at %s for %s", endpoint, entity_name)
        return None, False


__all__ = [
    "ENRICHMENT_CLASSIFICATIONS",
    "PROMPT_TEMPLATE",
    "HandlerCodeEnrichmentEffect",
]
