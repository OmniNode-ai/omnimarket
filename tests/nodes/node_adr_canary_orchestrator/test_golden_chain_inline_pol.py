# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Golden-chain inline proof-of-life for HandlerCanaryOrchestrator.

Runs the full ingestion → extraction → grading → draft-gen pipeline
in-process (no Kafka, no real LLM) using protocol-compliant mocks.

All mock signatures must match the orchestrator protocols exactly:
- ProtocolAdrGrading.grade() requires `source_summary` kwarg
- ProtocolAdrExtraction.extract() requires keyword-only args
- ProtocolAdrIngestion.ingest() requires positional root_paths list
- ProtocolAdrDraftGen.generate() requires keyword-only args

[OMN-10727]
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.models.adr import (
    ModelAdrDocumentRef,
    ModelAdrExtractionSummary,
    ModelAdrGradingScores,
    ModelAdrIngestionResult,
)
from omnimarket.nodes.node_adr_canary_orchestrator.handlers.handler_canary_orchestrator import (
    HandlerCanaryOrchestrator,
)
from omnimarket.nodes.node_adr_canary_orchestrator.models.model_canary_request import (
    ModelCanaryCommandPayload,
)

_GROUND_TRUTH_ADR = """\
# ADR-001: Use PostgreSQL for primary storage

## Status
Accepted

## Context
We need a reliable RDBMS for transactional data.

## Decision
Use PostgreSQL 15 as the primary database.

## Consequences
Team must be trained on PostgreSQL administration.
"""

_SOURCE_CONTENT = (
    "We evaluated several databases. PostgreSQL was chosen for reliability and "
    "team familiarity. The team will be trained on PostgreSQL administration."
)

_EXTRACTION_RAW: list[dict[str, object]] = [
    {
        "decision_type": "ARCHITECTURE",
        "statement": "Use PostgreSQL 15 as primary database",
        "rationale": "Reliability and team familiarity",
        "evidence_quotes": [
            "PostgreSQL was chosen for reliability and team familiarity."
        ],
        "confidence": 0.95,
    }
]


class _FakeIngestion:
    """Correctly-signed mock for ProtocolAdrIngestion."""

    def __init__(self, tmp_path: Path) -> None:
        self._tmp = tmp_path
        (tmp_path / "adr-001.md").write_text(_SOURCE_CONTENT, encoding="utf-8")

    async def ingest(self, root_paths: list[str]) -> ModelAdrIngestionResult:
        return ModelAdrIngestionResult(
            root_paths=root_paths,
            documents=[
                ModelAdrDocumentRef(
                    source_path="adr-001.md",
                    repo_name="omnimarket",
                    file_size_bytes=len(_SOURCE_CONTENT),
                    source_content_sha256="deadbeef",
                )
            ],
        )


class _FakeExtraction:
    """Correctly-signed mock for ProtocolAdrExtraction."""

    async def extract(
        self,
        *,
        ingestion: ModelAdrIngestionResult,
        model_key: str,
        model_id: str,
        correlation_id: str,
    ) -> ModelAdrExtractionSummary:
        return ModelAdrExtractionSummary(
            success=True,
            model_key=model_key,
            extraction_count=1,
            extractions_raw=_EXTRACTION_RAW,
            first_extraction_json=json.dumps(_EXTRACTION_RAW[0]),
        )


class _FakeGrading:
    """Correctly-signed mock for ProtocolAdrGrading.

    The `source_summary` kwarg is required by the orchestrator protocol.
    A mock that omits it causes a TypeError at call time, which is the bug
    this test gates against.
    """

    async def grade(
        self,
        *,
        ground_truth_adr: str,
        extraction: ModelAdrExtractionSummary,
        source_summary: str,
        correlation_id: str,
    ) -> ModelAdrGradingScores:
        assert source_summary, "source_summary must be non-empty"
        assert ground_truth_adr, "ground_truth_adr must be non-empty"
        return ModelAdrGradingScores(
            success=True,
            recall=0.90,
            precision=0.88,
            fidelity=0.92,
            format_compliance=1.0,
        )


class _FakeDraftGen:
    """Correctly-signed mock for ProtocolAdrDraftGen."""

    async def generate(
        self,
        *,
        extraction: ModelAdrExtractionSummary,
        run_id: str,
    ) -> str:
        statement = (
            extraction.extractions_raw[0].get("statement", "Untitled")
            if extraction.extractions_raw
            else "Untitled"
        )
        return (
            f"# ADR Draft — {run_id}\n\n"
            f"## Decision\n{statement}\n\n"
            "## Status\nProposed\n"
        )


class _FakeContainer:
    def __init__(self, services: dict[str, Any]) -> None:
        self._services = services

    def get_service(
        self,
        protocol_type: object,
        service_name: str | None = None,
    ) -> object:
        for key in (service_name, protocol_type):
            if key in self._services:
                return self._services[key]
        raise LookupError(service_name or str(protocol_type))

    def get_service_optional(
        self,
        protocol_type: object,
        service_name: str | None = None,
    ) -> object | None:
        try:
            return self.get_service(protocol_type, service_name=service_name)
        except LookupError:
            return None


def _make_manifest(tmp_path: Path) -> Path:
    manifest = {
        "entries": [
            {
                "id": "pol-entry-adr-001",
                "root_paths": [str(tmp_path)],
                "ground_truth_adr": _GROUND_TRUTH_ADR,
                "models": [
                    {
                        "key": "qwen3-coder",
                        "provider": "local",
                        "model_id": "test/qwen3-coder-30b",
                        "external": False,
                    },
                ],
            }
        ]
    }
    manifest_path = tmp_path / "ground_truth_manifest.yaml"
    manifest_path.write_text(yaml.dump(manifest), encoding="utf-8")
    return manifest_path


@pytest.mark.unit
@pytest.mark.asyncio
async def test_inline_pol_pipeline_completes_through_draft_gen(tmp_path: Path) -> None:
    """Full inline PoL: ingestion → extraction → grading → draft-gen all succeed."""
    manifest_path = _make_manifest(tmp_path)
    output_dir = tmp_path / "runs"

    container = _FakeContainer(
        {
            "ingestion": _FakeIngestion(tmp_path),
            "extraction": _FakeExtraction(),
            "grading": _FakeGrading(),
            "draft_gen": _FakeDraftGen(),
        }
    )
    handler = HandlerCanaryOrchestrator(container)

    report = await handler.handle(
        ModelCanaryCommandPayload(
            manifest_path=str(manifest_path),
            output_dir=str(output_dir),
        )
    )

    assert report.success is True, f"Pipeline failed: {report.error_message}"
    assert report.entries_total == 1
    assert report.entries_completed == 1
    assert report.entries_failed == 0
    assert len(report.model_scores) == 1

    score = report.model_scores[0]
    assert score.model_key == "qwen3-coder"
    assert score.entries_evaluated == 1
    assert score.avg_recall is not None
    assert abs((score.avg_recall or 0) - 0.90) < 1e-6

    # Verify draft file was generated
    evidence_dir = Path(report.evidence_dir)
    draft_files = list(evidence_dir.glob("pol-entry-adr-001/*_draft.md"))
    assert len(draft_files) == 1, (
        f"Expected 1 draft file, found: {[str(f) for f in draft_files]}"
    )
    draft_content = draft_files[0].read_text(encoding="utf-8")
    assert "ADR Draft" in draft_content
    assert "PostgreSQL" in draft_content or "Use PostgreSQL" in draft_content


@pytest.mark.unit
@pytest.mark.asyncio
async def test_grading_mock_accepts_source_summary_kwarg(tmp_path: Path) -> None:
    """Regression: grading mock must accept source_summary as a keyword arg.

    If the mock omits source_summary from its grade() signature, the orchestrator
    raises TypeError when it calls grade(source_summary=...), silently failing
    the grading stage and skipping draft-gen.
    """
    grader = _FakeGrading()
    extraction = ModelAdrExtractionSummary(
        success=True,
        model_key="qwen3-coder",
        extraction_count=1,
        extractions_raw=_EXTRACTION_RAW,
        first_extraction_json=json.dumps(_EXTRACTION_RAW[0]),
    )

    scores = await grader.grade(
        ground_truth_adr=_GROUND_TRUTH_ADR,
        extraction=extraction,
        source_summary="PostgreSQL was chosen for reliability.",
        correlation_id="test-corr-001",
    )

    assert scores.success is True
    assert scores.recall == pytest.approx(0.90)
    assert scores.precision == pytest.approx(0.88)
    assert scores.fidelity == pytest.approx(0.92)
    assert scores.format_compliance == pytest.approx(1.0)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_inline_pol_scorecard_written(tmp_path: Path) -> None:
    """Scorecard markdown must be written after a successful pipeline run."""
    manifest_path = _make_manifest(tmp_path)
    output_dir = tmp_path / "runs"

    container = _FakeContainer(
        {
            "ingestion": _FakeIngestion(tmp_path),
            "extraction": _FakeExtraction(),
            "grading": _FakeGrading(),
            "draft_gen": _FakeDraftGen(),
        }
    )
    handler = HandlerCanaryOrchestrator(container)

    report = await handler.handle(
        ModelCanaryCommandPayload(
            manifest_path=str(manifest_path),
            output_dir=str(output_dir),
        )
    )

    assert report.success is True
    scorecard = Path(report.scorecard_path)
    assert scorecard.exists(), f"Scorecard not found at {report.scorecard_path}"
    content = scorecard.read_text(encoding="utf-8")
    assert "qwen3-coder" in content
    assert "ADR Canary Scorecard" in content
