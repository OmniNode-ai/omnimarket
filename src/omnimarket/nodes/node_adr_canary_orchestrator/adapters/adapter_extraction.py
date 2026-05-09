# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""AdapterAdrExtraction — translates ProtocolAdrExtraction to node_adr_decision_extraction_llm_effect.

This adapter is the ONLY place that imports from the sibling extraction node's
private package. The orchestrator handler never imports sibling packages directly.

The sibling node expects ModelDocumentSegment items. We synthesise minimal
segments from the ingestion result's document refs (source_path + sha256 as
content proxy) so the pipeline can run end-to-end without a dedicated
segmentation node. A real deployment would wire a segmentation node here.
"""

from __future__ import annotations

import hashlib
import logging
from importlib import import_module

from omnimarket.models.adr import ModelAdrExtractionSummary, ModelAdrIngestionResult

logger = logging.getLogger(__name__)


class AdapterAdrExtraction:
    """Default ProtocolAdrExtraction implementation via HandlerDecisionExtraction."""

    async def extract(
        self,
        *,
        ingestion: ModelAdrIngestionResult,
        model_key: str,
        model_id: str,
        correlation_id: str,
    ) -> ModelAdrExtractionSummary:
        from omnimarket.nodes.node_adr_decision_extraction_llm_effect.handlers.handler_decision_extraction import (
            HandlerDecisionExtraction,
        )

        extraction_models = import_module(
            "omnimarket.nodes.node_adr_decision_extraction_llm_effect.models.model_extraction_request"
        )
        model_document_segment = extraction_models.ModelDocumentSegment
        model_extraction_request = extraction_models.ModelExtractionRequest

        if not ingestion.documents:
            return ModelAdrExtractionSummary(
                success=False,
                model_key=model_key,
                error_code="NO_DOCUMENTS",
                error_message="Ingestion returned no documents.",
            )

        # Synthesise one segment per document (source_path as content proxy).
        segments = []
        for doc in ingestion.documents:
            content_proxy = (
                f"source_path: {doc.source_path}\nsha256: {doc.source_content_sha256}"
            )
            seg_id = hashlib.sha256(
                f"{doc.source_path}:{doc.source_content_sha256}".encode()
            ).hexdigest()
            segments.append(
                model_document_segment(
                    segment_id=seg_id,
                    source_path=doc.source_path,
                    start_line=1,
                    end_line=1,
                    segment_type="document_ref",
                    content=content_proxy,
                    confidence=1.0,
                )
            )

        first_source = ingestion.documents[0].source_path if ingestion.documents else ""
        req = model_extraction_request(
            segments=segments,
            model_key=model_key,
            correlation_id=correlation_id,
            source_path=first_source,
        )

        handler = HandlerDecisionExtraction()
        result = await handler.handle(req)

        success = bool(getattr(result, "success", False))
        extractions = list(getattr(result, "extractions", []))
        raw = [
            e.model_dump() if hasattr(e, "model_dump") else dict(e) for e in extractions
        ]
        first_json = raw[0].__class__.__name__ if raw else ""
        try:
            import json

            first_json = json.dumps(raw[0]) if raw else ""
        except Exception:
            first_json = ""

        return ModelAdrExtractionSummary(
            success=success,
            model_key=model_key,
            extraction_count=len(extractions),
            extractions_raw=raw,
            first_extraction_json=first_json,
            error_code=getattr(result, "error_code", None),
            error_message=getattr(result, "error_message", None),
        )


__all__: list[str] = ["AdapterAdrExtraction"]
