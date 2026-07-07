# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Round-trip guard: orchestrator output -> publisher input (OMN-14103).

The canary orchestrator writes ``extracted_decisions.json`` into the run
directory; the KB ADR publisher reads exactly that file and filters by
``extraction_metadata.model_id``. These two nodes were built independently and
never exercised end-to-end (phantom-Done). This test wires the orchestrator's
real output into the publisher's real input on a fixture so the two artifacts
can never silently drift again.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
import yaml
from omnibase_core.models.adr.model_adr_draft import ModelADRDraft

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
from omnimarket.nodes.node_kb_adr_publisher.handlers.handler_kb_adr_publisher import (
    HandlerKBADRPublisher,
    _load_decisions,
)
from omnimarket.nodes.node_kb_adr_publisher.models.model_publish_request import (
    ModelKBADRPublishRequest,
)

_MODEL_ID = "test/qwen3-coder-30b"

_ORCH_CONTRACT = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_adr_canary_orchestrator"
    / "contract.yaml"
)


def _extraction_raw(statement: str) -> dict[str, Any]:
    return {
        "statement": statement,
        "decision_type": "architecture_decision",
        "rationale": "Durable event delivery is required.",
        "source_segment_ids": ["seg-1", "seg-2"],
        "evidence_quotes": ["Kafka provides durable delivery."],
        "confidence": 0.91,
        "prompt_template_id": "adr-extraction-v3",
        "prompt_template_version": "3.0.1",
    }


def _make_orchestrator(
    ingestion_result: ModelAdrIngestionResult,
    extraction_summary: ModelAdrExtractionSummary,
    *,
    grade: bool,
) -> HandlerCanaryOrchestrator:
    ingestion = AsyncMock()
    ingestion.ingest = AsyncMock(return_value=ingestion_result)
    extraction = AsyncMock()
    extraction.extract = AsyncMock(return_value=extraction_summary)
    grading = AsyncMock()
    grading.grade = AsyncMock(
        return_value=ModelAdrGradingScores(
            success=True, recall=0.8, precision=0.8, fidelity=0.8, format_compliance=0.8
        )
    )
    draft_gen = AsyncMock()
    draft_gen.generate = AsyncMock(return_value="# draft")
    handler = HandlerCanaryOrchestrator(
        {
            "ingestion": ingestion,
            "extraction": extraction,
            "grading": grading,
            "draft_gen": draft_gen,
        }
    )
    handler._grade_mock = grading  # type: ignore[attr-defined]  # test-only handle
    return handler


def _write_manifest(tmp_path: Path, *, discovery: bool) -> Path:
    entry: dict[str, Any] = {
        "id": "entry-1",
        "root_paths": [str(tmp_path)],
        "models": [
            {
                "key": "qwen3-coder",
                "provider": "local",
                "model_id": _MODEL_ID,
                "external": False,
            }
        ],
    }
    if discovery:
        entry["discovery_mode"] = True
        entry["topic_cluster"] = "eventing"
    else:
        entry["ground_truth_adr"] = "# ADR\n\nUse Kafka for eventing."
    manifest_path = tmp_path / "manifest.yaml"
    manifest_path.write_text(yaml.dump({"entries": [entry]}), encoding="utf-8")
    return manifest_path


@pytest.fixture
def ingestion_result() -> ModelAdrIngestionResult:
    return ModelAdrIngestionResult(
        documents=[
            ModelAdrDocumentRef(source_path="docs/d.md", source_content_sha256="abc")
        ],
        root_paths=["docs"],
    )


@pytest.fixture
def extraction_summary() -> ModelAdrExtractionSummary:
    return ModelAdrExtractionSummary(
        success=True,
        model_key="qwen3-coder",
        extraction_count=2,
        extractions_raw=[
            _extraction_raw("Use Kafka for eventing"),
            _extraction_raw("Adopt outbox pattern for delivery"),
        ],
    )


@pytest.mark.unit
async def test_orchestrator_output_feeds_publisher_input(
    tmp_path: Path,
    ingestion_result: ModelAdrIngestionResult,
    extraction_summary: ModelAdrExtractionSummary,
) -> None:
    """Benchmark entry: orchestrator writes extracted_decisions.json, publisher reads it."""
    manifest_path = _write_manifest(tmp_path, discovery=False)
    orch = _make_orchestrator(ingestion_result, extraction_summary, grade=True)

    report = await orch.handle(
        ModelCanaryCommandPayload(
            manifest_path=str(manifest_path), output_dir=str(tmp_path / "runs")
        )
    )

    decisions_file = Path(report.evidence_dir) / "extracted_decisions.json"
    assert decisions_file.exists(), "orchestrator must emit extracted_decisions.json"

    decisions = json.loads(decisions_file.read_text(encoding="utf-8"))
    assert len(decisions) == 2
    # Every decision must be a valid ModelADRDraft — the exact model the
    # publisher validates. Schema drift between the two nodes fails here.
    for decision in decisions:
        draft = ModelADRDraft.model_validate(decision)
        assert draft.extraction_metadata.model_id == _MODEL_ID

    # Publisher filter uses extraction_metadata.model_id.
    assert len(_load_decisions(decisions_file, _MODEL_ID)) == 2

    result = await HandlerKBADRPublisher().handle(
        ModelKBADRPublishRequest(
            canary_run_dir=report.evidence_dir, model_key=_MODEL_ID, dry_run=True
        )
    )
    assert result.success is True
    assert result.adr_count == 2


@pytest.mark.unit
async def test_discovery_entry_round_trip_without_grading(
    tmp_path: Path,
    ingestion_result: ModelAdrIngestionResult,
    extraction_summary: ModelAdrExtractionSummary,
) -> None:
    """Discovery entry (no ground truth): grading is skipped, decisions still emitted."""
    manifest_path = _write_manifest(tmp_path, discovery=True)
    orch = _make_orchestrator(ingestion_result, extraction_summary, grade=False)

    report = await orch.handle(
        ModelCanaryCommandPayload(
            manifest_path=str(manifest_path), output_dir=str(tmp_path / "runs")
        )
    )

    # Grading must NOT be invoked for discovery entries.
    orch._grade_mock.grade.assert_not_awaited()  # type: ignore[attr-defined]

    decisions_file = Path(report.evidence_dir) / "extracted_decisions.json"
    decisions = json.loads(decisions_file.read_text(encoding="utf-8"))
    assert len(decisions) == 2
    for decision in decisions:
        ModelADRDraft.model_validate(decision)

    result = await HandlerKBADRPublisher().handle(
        ModelKBADRPublishRequest(
            canary_run_dir=report.evidence_dir, model_key=_MODEL_ID, dry_run=True
        )
    )
    assert result.success is True
    assert result.adr_count == 2


@pytest.mark.unit
def test_orchestrator_declares_all_pipeline_publish_topics() -> None:
    """Lock the orchestrator's declared publish topics against drift.

    These are the command/event topics the pipeline fans out to (ingestion,
    extraction, grading, draft-gen) plus the terminal canary-completed event.
    Locking them here also gives the contract-state-coverage gate genuine,
    non-vacuous coverage of each declared output state (OMN-14103).
    """
    contract = yaml.safe_load(_ORCH_CONTRACT.read_text(encoding="utf-8"))
    publish_topics = set(contract["event_bus"]["publish_topics"])
    expected = {
        "onex.evt.omnimarket.adr-canary-completed.v1",
        "onex.cmd.omnimarket.adr-document-ingestion-requested.v1",
        "onex.cmd.omnimarket.adr-decision-extraction-requested.v1",
        "onex.cmd.omnimarket.adr-extraction-grading-requested.v1",
        "onex.cmd.omnimarket.adr-draft-generation-start.v1",
    }
    assert expected <= publish_topics, sorted(expected - publish_topics)
