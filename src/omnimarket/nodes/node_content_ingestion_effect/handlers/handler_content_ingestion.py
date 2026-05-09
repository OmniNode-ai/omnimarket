# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Handler for content ingestion — reads content-discovered events and extracts by type.

Content-type dispatch (python → AST, markdown → text, yaml → parse) is
handled in handler_routing.py code, not in contract YAML predicates.

[OMN-7873]
"""

from __future__ import annotations

import logging

from omnimarket.nodes.node_content_ingestion_effect.models.model_extraction_result import (
    ModelExtractionResult,
)
from omnimarket.nodes.node_content_ingestion_effect.models.model_ingestion_request import (
    ModelIngestionRequest,
)
from omnimarket.nodes.node_content_ingestion_effect.models.model_ingestion_summary import (
    ModelIngestionSummary,
)

logger = logging.getLogger(__name__)


class HandlerContentIngestion:
    """EFFECT handler — processes content-discovered events and emits extraction results.

    Read-only invariants: no filesystem writes, no LLM calls, no direct DB access.
    All persistence is downstream via projections from emitted events.
    """

    async def handle(
        self, *, request: ModelIngestionRequest
    ) -> list[ModelExtractionResult]:
        results: list[ModelExtractionResult] = []

        for source_path in request.source_paths:
            logger.info("Processing content source: %s", source_path)
            result = ModelExtractionResult(
                source_path=source_path,
                content_type=request.content_type,
                extracted_text=None,
                extraction_error="extraction_not_implemented",
            )
            results.append(result)

        return results

    async def summarize(
        self,
        *,
        results: list[ModelExtractionResult],
        request: ModelIngestionRequest,
    ) -> ModelIngestionSummary:
        succeeded = sum(
            1
            for r in results
            if r.extraction_error is None and r.extracted_text is not None
        )
        failed = len(results) - succeeded
        return ModelIngestionSummary(
            total_processed=len(results),
            succeeded=succeeded,
            failed=failed,
            correlation_id=request.correlation_id,
        )


__all__ = ["HandlerContentIngestion"]
