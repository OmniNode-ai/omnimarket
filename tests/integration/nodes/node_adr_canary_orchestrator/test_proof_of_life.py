# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Proof-of-life integration test for the ADR canary orchestrator pipeline.

Exercises the full end-to-end pipeline using deterministic stub protocol
adapters — no real LLM calls. Verifies:
  - Manifest loads and yields 3 entries
  - Ingestion, extraction, grading, and draft-gen stubs are invoked
  - Evidence JSON files are written for each entry/model
  - Scorecard markdown is written
  - ModelCanaryReport reflects success, entries_total == 3

[OMN-10699]
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

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

_MANIFEST_PATH = "docs/adr-canary/ground_truth_manifest.yaml"
_OMNIMARKET_ROOT = Path(__file__).parents[
    4
]  # tests/integration/nodes/node_adr_canary_orchestrator/ -> omnimarket root


# ---------------------------------------------------------------------------
# Stub protocol adapters
# ---------------------------------------------------------------------------


class _StubIngestion:
    def __init__(self) -> None:
        self.call_count = 0

    async def ingest(self, root_paths: list[str]) -> ModelAdrIngestionResult:
        self.call_count += 1
        return ModelAdrIngestionResult(
            documents=[
                ModelAdrDocumentRef(
                    source_path=f"{root_paths[0]}/stub_doc.md",
                    repo_name="omnibase_infra",
                    file_size_bytes=512,
                    source_content_sha256="abc123",
                )
            ],
            root_paths=root_paths,
        )


class _StubExtraction:
    def __init__(self) -> None:
        self.call_count = 0

    async def extract(
        self,
        *,
        ingestion: ModelAdrIngestionResult,
        model_key: str,
        model_id: str,
        correlation_id: str,
    ) -> ModelAdrExtractionSummary:
        self.call_count += 1
        return ModelAdrExtractionSummary(
            success=True,
            model_key=model_key,
            extraction_count=1,
            extractions_raw=[{"decision": "stub decision", "context": "stub context"}],
            first_extraction_json=json.dumps(
                {"decision": "stub decision", "context": "stub context"}
            ),
        )


class _StubGrading:
    def __init__(self) -> None:
        self.call_count = 0

    async def grade(
        self,
        *,
        ground_truth_adr: str,
        extraction: ModelAdrExtractionSummary,
        source_summary: str,
        correlation_id: str,
    ) -> ModelAdrGradingScores:
        self.call_count += 1
        return ModelAdrGradingScores(
            success=True,
            recall=0.85,
            precision=0.80,
            fidelity=0.90,
            format_compliance=1.0,
        )


class _StubDraftGen:
    def __init__(self) -> None:
        self.call_count = 0

    async def generate(
        self,
        *,
        extraction: ModelAdrExtractionSummary,
        run_id: str,
    ) -> str:
        self.call_count += 1
        return f"# ADR Draft\n\nRun: {run_id}\nModel: {extraction.model_key}\n\nStub draft content.\n"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_proof_of_life_full_pipeline() -> None:
    """Full pipeline: manifest loads, all stubs invoked, files written, report correct."""
    import asyncio

    ingestion_stub = _StubIngestion()
    extraction_stub = _StubExtraction()
    grading_stub = _StubGrading()
    draft_gen_stub = _StubDraftGen()

    container: dict[str, object] = {
        "ingestion": ingestion_stub,
        "extraction": extraction_stub,
        "grading": grading_stub,
        "draft_gen": draft_gen_stub,
    }

    handler = HandlerCanaryOrchestrator(container)

    manifest_abs = str(_OMNIMARKET_ROOT / _MANIFEST_PATH)

    with tempfile.TemporaryDirectory() as tmpdir:
        request = ModelCanaryCommandPayload(
            manifest_path=manifest_abs,
            output_dir=tmpdir,
        )
        report = asyncio.run(handler.handle(request))

        # Core report assertions
        assert report.success, f"Report failed: {report.error_message}"
        assert report.entries_total == 3
        assert report.entries_completed == 3
        assert report.entries_failed == 0

        # Scorecard written
        scorecard = Path(report.scorecard_path)
        assert scorecard.exists(), f"Scorecard not found at {scorecard}"
        scorecard_text = scorecard.read_text()
        assert "ADR Canary Scorecard" in scorecard_text

        # Evidence directory populated
        evidence_dir = Path(report.evidence_dir)
        assert evidence_dir.is_dir()

        # All 3 entry subdirs exist with at least one evidence JSON
        entry_ids = [
            "kafka-required-infrastructure",
            "vault-to-infisical-migration",
            "graceful-shutdown-drain-period",
        ]
        for entry_id in entry_ids:
            entry_dir = evidence_dir / entry_id
            assert entry_dir.is_dir(), f"Missing evidence dir for entry: {entry_id}"
            json_files = list(entry_dir.glob("*.json"))
            assert json_files, f"No evidence JSON files for entry: {entry_id}"
            for jf in json_files:
                evidence = json.loads(jf.read_text())
                assert evidence["extraction_success"] is True
                assert evidence["grading_success"] is True

    # Stubs were called at least once each
    assert ingestion_stub.call_count >= 3
    assert extraction_stub.call_count >= 3
    assert grading_stub.call_count >= 3
    assert draft_gen_stub.call_count >= 3

    # Model scores aggregated in report
    assert report.model_scores, "Expected model_scores in report"
    for score in report.model_scores:
        assert score.entries_evaluated > 0
        assert score.avg_recall is not None
        assert score.avg_precision is not None


@pytest.mark.integration
def test_proof_of_life_dry_run() -> None:
    """Dry-run mode: no stubs called, report returns entries_total == 3."""
    import asyncio

    container: dict[str, object] = {
        "ingestion": _StubIngestion(),
        "extraction": _StubExtraction(),
        "grading": _StubGrading(),
        "draft_gen": _StubDraftGen(),
    }

    handler = HandlerCanaryOrchestrator(container)
    manifest_abs = str(_OMNIMARKET_ROOT / _MANIFEST_PATH)

    with tempfile.TemporaryDirectory() as tmpdir:
        request = ModelCanaryCommandPayload(
            manifest_path=manifest_abs,
            output_dir=tmpdir,
            dry_run=True,
        )
        report = asyncio.run(handler.handle(request))

    assert report.dry_run is True
    assert report.entries_total == 3
    assert report.success is True


@pytest.mark.integration
def test_proof_of_life_extraction_failure_path() -> None:
    """Extraction failure: entry marked failed, pipeline continues for remaining entries."""
    import asyncio

    class _FailingExtraction:
        async def extract(
            self,
            *,
            ingestion: ModelAdrIngestionResult,
            model_key: str,
            model_id: str,
            correlation_id: str,
        ) -> ModelAdrExtractionSummary:
            raise RuntimeError("stub extraction failure")

    container: dict[str, object] = {
        "ingestion": _StubIngestion(),
        "extraction": _FailingExtraction(),
        "grading": _StubGrading(),
        "draft_gen": _StubDraftGen(),
    }

    handler = HandlerCanaryOrchestrator(container)
    manifest_abs = str(_OMNIMARKET_ROOT / _MANIFEST_PATH)

    with tempfile.TemporaryDirectory() as tmpdir:
        request = ModelCanaryCommandPayload(
            manifest_path=manifest_abs,
            output_dir=tmpdir,
        )
        report = asyncio.run(handler.handle(request))

        # Entries complete at the entry level (extraction failure is per model, not per entry)
        assert report.entries_total == 3
        # Evidence files should exist with extraction_error set
        evidence_dir = Path(report.evidence_dir)
        for entry_id in [
            "kafka-required-infrastructure",
            "vault-to-infisical-migration",
            "graceful-shutdown-drain-period",
        ]:
            entry_dir = evidence_dir / entry_id
            assert entry_dir.is_dir()
            json_files = list(entry_dir.glob("*.json"))
            assert json_files
            for jf in json_files:
                evidence = json.loads(jf.read_text())
                assert evidence["extraction_success"] is False
                assert evidence["extraction_error"] is not None
