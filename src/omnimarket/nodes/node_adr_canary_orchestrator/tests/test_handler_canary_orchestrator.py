# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Unit tests for HandlerCanaryOrchestrator.

All sub-handlers are mocked; no real LLM calls, no real filesystem beyond tmp_path.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

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
    ModelEvidenceRecord,
    _aggregate_scores,
    _make_run_id,
    _write_scorecard,
)
from omnimarket.nodes.node_adr_canary_orchestrator.models.model_canary_report import (
    ModelCanaryReport,
    ModelModelScore,
)
from omnimarket.nodes.node_adr_canary_orchestrator.models.model_canary_request import (
    ModelCanaryCommandPayload,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def minimal_manifest(tmp_path: Path) -> Path:
    manifest = {
        "entries": [
            {
                "id": "test-entry-1",
                "root_paths": [str(tmp_path)],
                "ground_truth_adr": "# ADR-001\n\nWe decided to use Kafka for messaging.",
                "models": [
                    {
                        "key": "qwen3-coder",
                        "provider": "local",
                        "model_id": "cyankiwi/Qwen3-Coder-30B",  # onex-allow-model-id OMN-10698 reason="test fixture model id exercises manifest parsing"
                        "external": False,
                    },
                ],
            }
        ]
    }
    manifest_path = tmp_path / "manifest.yaml"
    manifest_path.write_text(yaml.dump(manifest), encoding="utf-8")
    return manifest_path


@pytest.fixture
def mock_ingestion_result() -> ModelAdrIngestionResult:
    return ModelAdrIngestionResult(
        documents=[
            ModelAdrDocumentRef(
                source_path="docs/decisions/adr-001.md",
                source_content_sha256="abc123",
            )
        ],
        root_paths=["docs/decisions"],
    )


@pytest.fixture
def mock_extraction_result() -> ModelAdrExtractionSummary:
    return ModelAdrExtractionSummary(
        success=True,
        model_key="qwen3-coder",
        extraction_count=1,
        extractions_raw=[
            {
                "decision_type": "ARCHITECTURE",
                "statement": "Use Kafka",
                "evidence_quotes": ["Kafka provides durable event delivery."],
            }
        ],
        first_extraction_json=json.dumps(
            {
                "decision_type": "ARCHITECTURE",
                "statement": "Use Kafka",
                "evidence_quotes": ["Kafka provides durable event delivery."],
            }
        ),
    )


@pytest.fixture
def mock_grading_result() -> ModelAdrGradingScores:
    return ModelAdrGradingScores(
        success=True,
        recall=0.85,
        precision=0.90,
        fidelity=0.88,
        format_compliance=0.95,
    )


@pytest.fixture
def mock_draft_result() -> MagicMock:
    result = MagicMock()
    result.status = "ok"
    result.markdown = "# ADR-001\n\nDraft generated."
    return result


def _make_handler(
    tmp_path: Path,
    ingestion_result: Any,
    extraction_result: Any,
    grading_result: Any,
    draft_result: Any,
) -> HandlerCanaryOrchestrator:
    ingestion_handler = AsyncMock()
    ingestion_handler.ingest = AsyncMock(return_value=ingestion_result)

    extraction_handler = AsyncMock()
    extraction_handler.extract = AsyncMock(return_value=extraction_result)

    grader_handler = AsyncMock()
    grader_handler.grade = AsyncMock(return_value=grading_result)

    draft_gen_handler = AsyncMock()
    draft_gen_handler.generate = AsyncMock(return_value=draft_result.markdown)

    return HandlerCanaryOrchestrator(
        ingestion_handler=ingestion_handler,
        extraction_handler=extraction_handler,
        grader_handler=grader_handler,
        draft_gen_handler=draft_gen_handler,
        max_concurrent_extractions=2,
        grader_model_key="opus",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestModelCanaryCommandPayload:
    def test_defaults(self) -> None:
        payload = ModelCanaryCommandPayload()
        assert payload.manifest_path == "docs/adr-canary/ground_truth_manifest.yaml"
        assert payload.model_subset is None
        assert payload.output_dir == "docs/adr-canary-runs/"
        assert payload.dry_run is False
        assert payload.resume_run_id is None
        assert payload.max_cost_usd is None
        assert payload.allow_external_providers is False

    def test_model_subset_specified(self) -> None:
        payload = ModelCanaryCommandPayload(model_subset=["qwen3-coder", "deepseek-r1"])
        assert payload.model_subset == ["qwen3-coder", "deepseek-r1"]

    def test_frozen(self) -> None:
        from pydantic import ValidationError

        payload = ModelCanaryCommandPayload()
        with pytest.raises((ValidationError, TypeError)):
            payload.dry_run = True  # type: ignore[misc]


class TestMakeRunId:
    def test_format(self) -> None:
        run_id = _make_run_id()
        parts = run_id.split("-")
        # YYYYMMDD-HHMMSS-<random6>
        assert len(parts) == 3
        assert len(parts[0]) == 8  # YYYYMMDD
        assert len(parts[1]) == 6  # HHMMSS
        assert len(parts[2]) == 6  # random suffix

    def test_unique(self) -> None:
        ids = {_make_run_id() for _ in range(20)}
        assert len(ids) >= 18


class TestAggregateScores:
    def test_empty(self) -> None:
        assert _aggregate_scores([]) == []

    def test_single_model_success(self) -> None:
        records = [
            ModelEvidenceRecord(
                run_id="r1",
                entry_id="e1",
                model_key="qwen3",
                model_id="m1",
                recall=0.8,
                precision=0.9,
                fidelity=0.85,
                format_compliance=0.95,
                extraction_success=True,
                grading_success=True,
                latency_ms=500,
            )
        ]
        scores = _aggregate_scores(records)
        assert len(scores) == 1
        s = scores[0]
        assert s.model_key == "qwen3"
        assert s.entries_evaluated == 1
        assert s.entries_failed == 0
        assert s.avg_recall == pytest.approx(0.8)
        assert s.total_latency_ms == 500

    def test_multiple_models(self) -> None:
        records = [
            ModelEvidenceRecord(
                run_id="r1",
                entry_id="e1",
                model_key="model-a",
                model_id="m-a",
                recall=0.7,
                precision=0.8,
                fidelity=0.75,
                format_compliance=0.9,
                extraction_success=True,
                grading_success=True,
                latency_ms=300,
            ),
            ModelEvidenceRecord(
                run_id="r1",
                entry_id="e1",
                model_key="model-b",
                model_id="m-b",
                extraction_success=False,
                grading_success=False,
                extraction_error="LLM timeout",
                latency_ms=100,
            ),
        ]
        scores = _aggregate_scores(records)
        assert len(scores) == 2
        by_key = {s.model_key: s for s in scores}
        assert by_key["model-a"].entries_evaluated == 1
        assert by_key["model-b"].entries_evaluated == 0
        assert by_key["model-b"].entries_failed == 1
        assert by_key["model-b"].avg_recall is None

    def test_avg_across_entries(self) -> None:
        records = [
            ModelEvidenceRecord(
                run_id="r1",
                entry_id="e1",
                model_key="m",
                model_id="mid",
                recall=0.6,
                grading_success=True,
                latency_ms=100,
            ),
            ModelEvidenceRecord(
                run_id="r1",
                entry_id="e2",
                model_key="m",
                model_id="mid",
                recall=0.8,
                grading_success=True,
                latency_ms=200,
            ),
        ]
        scores = _aggregate_scores(records)
        assert len(scores) == 1
        assert scores[0].avg_recall == pytest.approx(0.7)
        assert scores[0].total_latency_ms == 300


class TestWriteScorecard:
    def test_creates_file(self, tmp_path: Path) -> None:
        scores = [
            ModelModelScore(
                model_key="qwen3-coder",
                entries_evaluated=2,
                entries_failed=0,
                avg_recall=0.85,
                avg_precision=0.90,
                avg_fidelity=0.88,
                avg_format_compliance=0.95,
                total_latency_ms=1200,
            )
        ]
        path = _write_scorecard(
            run_id="20260508-120000-abcdef",
            manifest_path="docs/adr-canary/ground_truth_manifest.yaml",
            scores=scores,
            evidence_dir=tmp_path,
            entries_total=2,
            entries_completed=2,
            entries_failed=0,
        )
        assert path.exists()
        content = path.read_text()
        assert "qwen3-coder" in content
        assert "0.850" in content
        assert "ADR Canary Scorecard" in content


class TestHandlerCanaryOrchestrator:
    def test_dry_run(self, tmp_path: Path, minimal_manifest: Path) -> None:
        handler = HandlerCanaryOrchestrator()
        payload = ModelCanaryCommandPayload(
            manifest_path=str(minimal_manifest),
            output_dir=str(tmp_path / "runs"),
            dry_run=True,
        )
        result = asyncio.get_event_loop().run_until_complete(handler.handle(payload))
        assert isinstance(result, ModelCanaryReport)
        assert result.dry_run is True
        assert result.entries_total == 1
        assert result.success is True

    def test_dry_run_bad_manifest(self, tmp_path: Path) -> None:
        handler = HandlerCanaryOrchestrator()
        payload = ModelCanaryCommandPayload(
            manifest_path=str(tmp_path / "nonexistent.yaml"),
            output_dir=str(tmp_path / "runs"),
            dry_run=True,
        )
        result = asyncio.get_event_loop().run_until_complete(handler.handle(payload))
        assert result.success is False
        assert result.error_message is not None

    def test_full_pipeline_success(
        self,
        tmp_path: Path,
        minimal_manifest: Path,
        mock_ingestion_result: Any,
        mock_extraction_result: Any,
        mock_grading_result: Any,
        mock_draft_result: Any,
    ) -> None:
        handler = _make_handler(
            tmp_path,
            mock_ingestion_result,
            mock_extraction_result,
            mock_grading_result,
            mock_draft_result,
        )
        payload = ModelCanaryCommandPayload(
            manifest_path=str(minimal_manifest),
            output_dir=str(tmp_path / "runs"),
        )

        result = asyncio.get_event_loop().run_until_complete(handler.handle(payload))

        assert isinstance(result, ModelCanaryReport)
        assert result.entries_total == 1
        assert result.success is True
        assert result.scorecard_path.endswith("scorecard.md")
        assert Path(result.scorecard_path).exists()

    def test_external_provider_blocked(self, tmp_path: Path) -> None:
        manifest = {
            "entries": [
                {
                    "id": "ext-entry",
                    "root_paths": [str(tmp_path)],
                    "ground_truth_adr": "# ADR",
                    "models": [
                        {
                            "key": "claude-opus",
                            "provider": "anthropic",
                            "model_id": "claude-opus-4",
                            "external": True,
                        }
                    ],
                }
            ]
        }
        manifest_path = tmp_path / "ext_manifest.yaml"
        manifest_path.write_text(yaml.dump(manifest), encoding="utf-8")

        mock_ingest = AsyncMock()
        mock_ingest_result = ModelAdrIngestionResult(documents=[], root_paths=[])
        mock_ingest.ingest = AsyncMock(return_value=mock_ingest_result)

        handler = HandlerCanaryOrchestrator(
            ingestion_handler=mock_ingest,
            allow_external_providers=False,
        )

        result = asyncio.get_event_loop().run_until_complete(
            handler.handle(
                ModelCanaryCommandPayload(
                    manifest_path=str(manifest_path),
                    output_dir=str(tmp_path / "runs"),
                )
            )
        )

        assert isinstance(result, ModelCanaryReport)
        assert result.model_scores[0].entries_evaluated == 0

    def test_model_subset_filter(self, tmp_path: Path) -> None:
        manifest = {
            "entries": [
                {
                    "id": "filter-entry",
                    "root_paths": [str(tmp_path)],
                    "ground_truth_adr": "# ADR",
                    "models": [
                        {
                            "key": "model-a",
                            "provider": "local",
                            "model_id": "m-a",
                            "external": False,
                        },
                        {
                            "key": "model-b",
                            "provider": "local",
                            "model_id": "m-b",
                            "external": False,
                        },
                    ],
                }
            ]
        }
        manifest_path = tmp_path / "subset_manifest.yaml"
        manifest_path.write_text(yaml.dump(manifest), encoding="utf-8")

        mock_ingest = AsyncMock()
        mock_ingest_result = ModelAdrIngestionResult(
            documents=[
                ModelAdrDocumentRef(
                    source_path="docs/decisions/adr-001.md",
                    source_content_sha256="abc123",
                )
            ],
            root_paths=["docs/decisions"],
        )
        mock_ingest.ingest = AsyncMock(return_value=mock_ingest_result)

        mock_extract = AsyncMock()
        mock_extract_result = ModelAdrExtractionSummary(
            success=True,
            model_key="model-a",
            extraction_count=0,
        )
        mock_extract.extract = AsyncMock(return_value=mock_extract_result)

        mock_grader = AsyncMock()
        mock_grader_result = ModelAdrGradingScores(
            success=True,
            recall=0.8,
            precision=0.8,
            fidelity=0.8,
            format_compliance=0.8,
        )
        mock_grader.grade = AsyncMock(return_value=mock_grader_result)

        mock_draft = AsyncMock()
        mock_draft.generate = AsyncMock(return_value="")

        handler = HandlerCanaryOrchestrator(
            ingestion_handler=mock_ingest,
            extraction_handler=mock_extract,
            grader_handler=mock_grader,
            draft_gen_handler=mock_draft,
        )

        result = asyncio.get_event_loop().run_until_complete(
            handler.handle(
                ModelCanaryCommandPayload(
                    manifest_path=str(manifest_path),
                    output_dir=str(tmp_path / "runs"),
                    model_subset=["model-a"],
                )
            )
        )

        scored_models = {s.model_key for s in result.model_scores}
        assert "model-a" in scored_models
        assert "model-b" not in scored_models

    def test_from_contract(self) -> None:
        contract = {
            "config": {
                "max_concurrent_extractions": {"default": 8},
                "grader_model_key": {"default": "deepseek-r1"},
                "allow_external_providers": {"default": True},
            }
        }
        handler = HandlerCanaryOrchestrator.from_contract(contract)
        assert handler._max_concurrent_extractions == 8  # noqa: SLF001
        assert handler._grader_model_key == "deepseek-r1"  # noqa: SLF001
        assert handler._allow_external_providers is True  # noqa: SLF001

    def test_from_contract_defaults(self) -> None:
        handler = HandlerCanaryOrchestrator.from_contract({})
        assert handler._max_concurrent_extractions == 4  # noqa: SLF001
        assert handler._grader_model_key == "opus"  # noqa: SLF001
        assert handler._allow_external_providers is False  # noqa: SLF001

    def test_evidence_json_written(
        self,
        tmp_path: Path,
        minimal_manifest: Path,
        mock_ingestion_result: Any,
        mock_extraction_result: Any,
        mock_grading_result: Any,
        mock_draft_result: Any,
    ) -> None:
        handler = _make_handler(
            tmp_path,
            mock_ingestion_result,
            mock_extraction_result,
            mock_grading_result,
            mock_draft_result,
        )
        payload = ModelCanaryCommandPayload(
            manifest_path=str(minimal_manifest),
            output_dir=str(tmp_path / "runs"),
        )

        result = asyncio.get_event_loop().run_until_complete(handler.handle(payload))

        evidence_dir = Path(result.evidence_dir)
        entry_dir = evidence_dir / "test-entry-1"
        evidence_file = entry_dir / "qwen3-coder.json"
        assert evidence_file.exists()
        data = json.loads(evidence_file.read_text())
        assert data["model_key"] == "qwen3-coder"
        assert data["entry_id"] == "test-entry-1"
