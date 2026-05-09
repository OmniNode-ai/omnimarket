# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""AdapterAdrGrading — translates ProtocolAdrGrading to node_adr_extraction_grader_llm_effect.

This adapter is the ONLY place that imports from the sibling grader node's
private package. The orchestrator handler never imports sibling packages directly.
"""

from __future__ import annotations

import logging
import time
from importlib import import_module

from omnimarket.models.adr import ModelAdrExtractionSummary, ModelAdrGradingScores

logger = logging.getLogger(__name__)


class AdapterAdrGrading:
    """Default ProtocolAdrGrading implementation via HandlerExtractionGrader."""

    async def grade(
        self,
        *,
        ground_truth_adr: str,
        extraction: ModelAdrExtractionSummary,
        source_summary: str,
        correlation_id: str,
    ) -> ModelAdrGradingScores:
        from omnimarket.nodes.node_adr_extraction_grader_llm_effect.handlers.handler_extraction_grader import (
            HandlerExtractionGrader,
        )

        grading_models = import_module(
            "omnimarket.nodes.node_adr_extraction_grader_llm_effect.models.model_grading_request"
        )
        model_grading_request = grading_models.ModelGradingRequest

        t0 = time.monotonic()
        req = model_grading_request(
            ground_truth_adr=ground_truth_adr,
            extraction_output=extraction.extractions_raw,
            source_document=source_summary,
            correlation_id=correlation_id,
            model_key_under_test=extraction.model_key,
        )

        handler = HandlerExtractionGrader()
        result = await handler.handle(req)
        latency_ms = int((time.monotonic() - t0) * 1000)

        return ModelAdrGradingScores(
            success=bool(getattr(result, "success", False)),
            recall=getattr(result, "recall", None),
            precision=getattr(result, "precision", None),
            fidelity=getattr(result, "fidelity", None),
            format_compliance=getattr(result, "format_compliance", None),
            error_code=getattr(result, "error_code", None),
            error_message=getattr(result, "error_message", None),
            latency_ms=latency_ms,
        )


__all__: list[str] = ["AdapterAdrGrading"]
