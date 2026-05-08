"""Tests for HandlerADRGeneration — deterministic ADR markdown rendering."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_adr_draft_generation_compute.handlers.handler_adr_generation import (
    HandlerADRGeneration,
)
from omnimarket.nodes.node_adr_draft_generation_compute.models.model_decision_extraction import (
    EnumDecisionType,
    ModelDecisionExtraction,
    ModelExtractionProvenance,
)
from omnimarket.nodes.node_adr_draft_generation_compute.models.model_generation_request import (
    ModelADRGenerationRequest,
)
from omnimarket.nodes.node_adr_draft_generation_compute.models.model_generation_result import (
    EnumGenerationStatus,
)

_UNSET: list[str] = []


def _make_extraction(
    *,
    extraction_id: str = "ext-001",
    title: str = "Use Kafka for event streaming",
    decision_type: EnumDecisionType = EnumDecisionType.ARCHITECTURE,
    rationale_bullets: list[str] | None = None,
    consequences: list[str] | None = None,
    alternatives_considered: list[str] = _UNSET,
    model_id: str = "test-model-v1",
    confidence: float = 0.87,
    canary_run_id: str = "canary-run-abc",
    source_doc_paths: list[str] | None = None,
    prompt_template_id: str = "tmpl-001",
    prompt_template_version: str = "1.2.0",
    pipeline_version: str = "0.5.0",
    timestamp: str = "2026-05-08T10:00:00Z",
) -> ModelDecisionExtraction:
    _alts = (
        ["Direct HTTP calls"]
        if alternatives_considered is _UNSET
        else alternatives_considered
    )
    return ModelDecisionExtraction(
        extraction_id=extraction_id,
        title=title,
        decision_type=decision_type,
        rationale_bullets=rationale_bullets or ["Decouples producers from consumers"],
        consequences=consequences or ["Requires broker operations"],
        alternatives_considered=_alts,
        model_id=model_id,
        confidence=confidence,
        canary_run_id=canary_run_id,
        provenance=ModelExtractionProvenance(
            source_doc_paths=source_doc_paths or ["docs/adr/0001-kafka.md"],
            prompt_template_id=prompt_template_id,
            prompt_template_version=prompt_template_version,
            pipeline_version=pipeline_version,
            timestamp=timestamp,
        ),
    )


@pytest.mark.unit
class TestHandlerADRGenerationStructure:
    def test_result_status_ok(self) -> None:
        extraction = _make_extraction()
        request = ModelADRGenerationRequest(extraction=extraction, run_id="run-001")
        result = HandlerADRGeneration().handle(request)
        assert result.status == EnumGenerationStatus.OK

    def test_result_carries_extraction_id(self) -> None:
        extraction = _make_extraction(extraction_id="ext-xyz")
        request = ModelADRGenerationRequest(extraction=extraction, run_id="run-001")
        result = HandlerADRGeneration().handle(request)
        assert result.extraction_id == "ext-xyz"

    def test_result_carries_run_id(self) -> None:
        extraction = _make_extraction()
        request = ModelADRGenerationRequest(extraction=extraction, run_id="run-007")
        result = HandlerADRGeneration().handle(request)
        assert result.run_id == "run-007"

    def test_markdown_has_adr_title_heading(self) -> None:
        extraction = _make_extraction(title="Use Postgres for persistence")
        request = ModelADRGenerationRequest(extraction=extraction)
        result = HandlerADRGeneration().handle(request)
        assert "# ADR: Use Postgres for persistence" in result.markdown

    def test_markdown_has_proposed_status(self) -> None:
        extraction = _make_extraction()
        request = ModelADRGenerationRequest(extraction=extraction)
        result = HandlerADRGeneration().handle(request)
        assert "**Status**: Proposed" in result.markdown

    def test_markdown_has_iso_date_from_timestamp(self) -> None:
        # Date is derived from provenance.timestamp, not the wall clock.
        extraction = _make_extraction(timestamp="2026-05-08T10:00:00Z")
        request = ModelADRGenerationRequest(extraction=extraction)
        result = HandlerADRGeneration().handle(request)
        assert "**Date**: 2026-05-08" in result.markdown

    def test_adr_date_field_overrides_timestamp(self) -> None:
        extraction = _make_extraction(timestamp="2026-05-08T10:00:00Z")
        request = ModelADRGenerationRequest(
            extraction=extraction, adr_date="2025-01-01"
        )
        result = HandlerADRGeneration().handle(request)
        assert "**Date**: 2025-01-01" in result.markdown

    def test_markdown_has_related_section(self) -> None:
        extraction = _make_extraction(source_doc_paths=["docs/adr/0001-kafka.md"])
        request = ModelADRGenerationRequest(extraction=extraction)
        result = HandlerADRGeneration().handle(request)
        assert "**Related**:" in result.markdown
        assert "docs/adr/0001-kafka.md" in result.markdown

    def test_markdown_has_extraction_model(self) -> None:
        extraction = _make_extraction(model_id="opus-4-canary")
        request = ModelADRGenerationRequest(extraction=extraction)
        result = HandlerADRGeneration().handle(request)
        assert "**Extraction Model**: opus-4-canary" in result.markdown

    def test_markdown_has_confidence(self) -> None:
        extraction = _make_extraction(confidence=0.92)
        request = ModelADRGenerationRequest(extraction=extraction)
        result = HandlerADRGeneration().handle(request)
        assert "**Confidence**: 0.92" in result.markdown

    def test_markdown_has_canary_run_id(self) -> None:
        extraction = _make_extraction(canary_run_id="canary-run-xyz")
        request = ModelADRGenerationRequest(extraction=extraction)
        result = HandlerADRGeneration().handle(request)
        assert "**Canary Run ID**: canary-run-xyz" in result.markdown

    def test_markdown_has_context_section(self) -> None:
        extraction = _make_extraction(
            rationale_bullets=["High throughput needed", "Fan-out to many consumers"]
        )
        request = ModelADRGenerationRequest(extraction=extraction)
        result = HandlerADRGeneration().handle(request)
        assert "## Context" in result.markdown
        assert "High throughput needed" in result.markdown
        assert "Fan-out to many consumers" in result.markdown

    def test_markdown_has_decision_section(self) -> None:
        extraction = _make_extraction(
            title="Use Kafka", decision_type=EnumDecisionType.TECHNOLOGY
        )
        request = ModelADRGenerationRequest(extraction=extraction)
        result = HandlerADRGeneration().handle(request)
        assert "## Decision" in result.markdown
        assert "Use Kafka" in result.markdown
        assert "TECHNOLOGY" in result.markdown

    def test_markdown_has_consequences_section(self) -> None:
        extraction = _make_extraction(consequences=["Ops overhead", "Latency benefits"])
        request = ModelADRGenerationRequest(extraction=extraction)
        result = HandlerADRGeneration().handle(request)
        assert "## Consequences" in result.markdown
        assert "Ops overhead" in result.markdown
        assert "Latency benefits" in result.markdown

    def test_markdown_has_alternatives_section_when_present(self) -> None:
        extraction = _make_extraction(alternatives_considered=["RabbitMQ", "SQS"])
        request = ModelADRGenerationRequest(extraction=extraction)
        result = HandlerADRGeneration().handle(request)
        assert "## Alternatives Considered" in result.markdown
        assert "RabbitMQ" in result.markdown
        assert "SQS" in result.markdown

    def test_markdown_omits_alternatives_section_when_empty(self) -> None:
        extraction = _make_extraction(alternatives_considered=[])
        request = ModelADRGenerationRequest(extraction=extraction)
        result = HandlerADRGeneration().handle(request)
        assert "## Alternatives Considered" not in result.markdown

    def test_markdown_has_source_evidence_section(self) -> None:
        extraction = _make_extraction(source_doc_paths=["docs/adr/0002-db.md"])
        request = ModelADRGenerationRequest(extraction=extraction)
        result = HandlerADRGeneration().handle(request)
        assert "## Source Evidence" in result.markdown
        assert "docs/adr/0002-db.md" in result.markdown

    def test_markdown_has_extraction_metadata_section(self) -> None:
        extraction = _make_extraction(
            pipeline_version="0.5.0",
            prompt_template_id="tmpl-001",
            prompt_template_version="1.2.0",
            timestamp="2026-05-08T10:00:00Z",
        )
        request = ModelADRGenerationRequest(extraction=extraction)
        result = HandlerADRGeneration().handle(request)
        assert "## Extraction Metadata" in result.markdown
        assert "0.5.0" in result.markdown
        assert "tmpl-001" in result.markdown
        assert "1.2.0" in result.markdown
        assert "2026-05-08T10:00:00Z" in result.markdown


@pytest.mark.unit
class TestHandlerADRGenerationDeterminism:
    def test_same_input_produces_identical_output(self) -> None:
        extraction = _make_extraction()
        request = ModelADRGenerationRequest(extraction=extraction, run_id="run-det")
        handler = HandlerADRGeneration()
        result_a = handler.handle(request)
        result_b = handler.handle(request)
        assert result_a.markdown == result_b.markdown

    def test_different_titles_produce_different_output(self) -> None:
        req_a = ModelADRGenerationRequest(
            extraction=_make_extraction(title="Use Kafka"),
            run_id="run-1",
        )
        req_b = ModelADRGenerationRequest(
            extraction=_make_extraction(title="Use RabbitMQ"),
            run_id="run-1",
        )
        handler = HandlerADRGeneration()
        assert handler.handle(req_a).markdown != handler.handle(req_b).markdown

    def test_different_run_ids_produce_different_output(self) -> None:
        extraction = _make_extraction()
        req_a = ModelADRGenerationRequest(extraction=extraction, run_id="run-aaa")
        req_b = ModelADRGenerationRequest(extraction=extraction, run_id="run-bbb")
        handler = HandlerADRGeneration()
        assert handler.handle(req_a).markdown != handler.handle(req_b).markdown

    def test_no_error_field_on_success(self) -> None:
        extraction = _make_extraction()
        request = ModelADRGenerationRequest(extraction=extraction)
        result = HandlerADRGeneration().handle(request)
        assert result.error is None
