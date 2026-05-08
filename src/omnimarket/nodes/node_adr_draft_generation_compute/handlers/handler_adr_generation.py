"""Deterministic ADR markdown renderer — no LLM calls, same input = identical output."""

from __future__ import annotations

import datetime

from omnimarket.nodes.node_adr_draft_generation_compute.models.model_generation_request import (
    ModelADRGenerationRequest,
)
from omnimarket.nodes.node_adr_draft_generation_compute.models.model_generation_result import (
    EnumGenerationStatus,
    ModelADRGenerationResult,
)

_TODAY = datetime.date.today().isoformat()


class HandlerADRGeneration:
    """Render ADR markdown from a ModelDecisionExtraction. Pure, deterministic, no I/O."""

    def handle(self, request: ModelADRGenerationRequest) -> ModelADRGenerationResult:
        ext = request.extraction
        lines: list[str] = []

        # Title + header fields
        lines.append(f"# ADR: {ext.title}")
        lines.append("")
        lines.append("**Status**: Proposed")
        lines.append(f"**Date**: {_TODAY}")

        related = ", ".join(ext.provenance.source_doc_paths)
        lines.append(f"**Related**: {related}")
        lines.append(f"**Extraction Model**: {ext.model_id}")
        lines.append(f"**Confidence**: {ext.confidence}")
        lines.append(f"**Canary Run ID**: {ext.canary_run_id}")
        if request.run_id:
            lines.append(f"**Run ID**: {request.run_id}")
        lines.append("")

        # Context
        lines.append("## Context")
        lines.append("")
        for bullet in ext.rationale_bullets:
            lines.append(f"- {bullet}")
        lines.append("")

        # Decision
        lines.append("## Decision")
        lines.append("")
        lines.append(f"**{ext.title}** (type: {ext.decision_type.value})")
        lines.append("")

        # Consequences
        lines.append("## Consequences")
        lines.append("")
        for item in ext.consequences:
            lines.append(f"- {item}")
        lines.append("")

        # Alternatives Considered (omit section entirely if empty)
        if ext.alternatives_considered:
            lines.append("## Alternatives Considered")
            lines.append("")
            for alt in ext.alternatives_considered:
                lines.append(f"- {alt}")
            lines.append("")

        # Source Evidence
        lines.append("## Source Evidence")
        lines.append("")
        for path in ext.provenance.source_doc_paths:
            lines.append(f"- {path}")
        lines.append("")

        # Extraction Metadata
        lines.append("## Extraction Metadata")
        lines.append("")
        lines.append(f"- pipeline_version: {ext.provenance.pipeline_version}")
        lines.append(f"- model_id: {ext.model_id}")
        lines.append(f"- confidence: {ext.confidence}")
        lines.append(f"- timestamp: {ext.provenance.timestamp}")
        lines.append(f"- prompt_template_id: {ext.provenance.prompt_template_id}")
        lines.append(
            f"- prompt_template_version: {ext.provenance.prompt_template_version}"
        )
        lines.append("")

        return ModelADRGenerationResult(
            status=EnumGenerationStatus.OK,
            extraction_id=ext.extraction_id,
            run_id=request.run_id,
            markdown="\n".join(lines),
        )


__all__ = ["HandlerADRGeneration"]
