# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""AdapterAdrDraftGen — translates ProtocolAdrDraftGen to node_adr_draft_generation_compute.

This adapter is the ONLY place that imports from the sibling draft-gen node's
private package. The orchestrator handler never imports sibling packages directly.

The draft-gen node requires a ModelDecisionExtraction (the local stand-in type
from node_adr_draft_generation_compute.models.model_decision_extraction).
We build a minimal instance from the ExtractionSummary's first_extraction_json.
"""

from __future__ import annotations

import json
import logging
from importlib import import_module

from omnimarket.models.adr import ModelAdrExtractionSummary

logger = logging.getLogger(__name__)


class AdapterAdrDraftGen:
    """Default ProtocolAdrDraftGen implementation via HandlerADRGeneration."""

    async def generate(
        self,
        *,
        extraction: ModelAdrExtractionSummary,
        run_id: str,
    ) -> str:
        from omnimarket.nodes.node_adr_draft_generation_compute.handlers.handler_adr_generation import (
            HandlerADRGeneration,
        )

        decision_models = import_module(
            "omnimarket.nodes.node_adr_draft_generation_compute.models.model_decision_extraction"
        )
        request_models = import_module(
            "omnimarket.nodes.node_adr_draft_generation_compute.models.model_generation_request"
        )
        enum_decision_type = decision_models.EnumDecisionType
        model_decision_extraction = decision_models.ModelDecisionExtraction
        model_extraction_provenance = decision_models.ModelExtractionProvenance
        model_adr_generation_request = request_models.ModelADRGenerationRequest

        # Build a minimal ModelDecisionExtraction from the raw extraction summary.
        # If parsing fails, return an empty string (draft skipped — non-fatal).
        try:
            first_raw: dict[str, object] = {}
            if extraction.first_extraction_json:
                first_raw = json.loads(extraction.first_extraction_json)

            title = str(first_raw.get("statement", first_raw.get("title", "ADR")))
            # Map extraction_node decision_type enum to draft_gen local enum
            raw_dt = str(first_raw.get("decision_type", "ARCHITECTURE"))
            try:
                dt = enum_decision_type(raw_dt.upper())
            except ValueError:
                dt = enum_decision_type.ARCHITECTURE

            raw_quotes = first_raw.get("evidence_quotes", [])
            evidence_quotes = raw_quotes if isinstance(raw_quotes, list) else []

            dec = model_decision_extraction(
                extraction_id=str(first_raw.get("extraction_id", run_id)),
                title=title,
                decision_type=dt,
                rationale_bullets=[str(r) for r in evidence_quotes],
                model_id=extraction.model_key,
                provenance=model_extraction_provenance(),
                canary_run_id=run_id,
            )
        except Exception as exc:
            logger.warning("Draft-gen extraction parse failed: %s", exc)
            return ""

        handler = HandlerADRGeneration()
        result = handler.handle(
            model_adr_generation_request(extraction=dec, run_id=run_id)
        )

        if getattr(result, "status", "error") == "ok":
            return str(getattr(result, "markdown", ""))
        return ""


__all__: list[str] = ["AdapterAdrDraftGen"]
