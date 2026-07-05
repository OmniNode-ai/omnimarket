# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Unit tests for HandlerCanaryOrchestrator.

All sub-handlers are mocked; no real LLM calls, no real filesystem beyond tmp_path.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
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


class FakeContainer:
    def __init__(self, services: Mapping[Any, object]) -> None:
        self._services = dict(services)

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
                        "model_id": "test/qwen3-coder-30b",
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

    handler = HandlerCanaryOrchestrator(
        FakeContainer(
            {
                "ingestion": ingestion_handler,
                "extraction": extraction_handler,
                "grading": grader_handler,
                "draft_gen": draft_gen_handler,
            }
        )
    )
    handler._max_concurrent_extractions = 2  # noqa: SLF001
    handler._grader_model_key = "opus"  # noqa: SLF001
    return handler


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


def _stub_handler() -> HandlerCanaryOrchestrator:
    """Minimal handler with no-op mocks for tests that only exercise dry_run."""
    return HandlerCanaryOrchestrator(
        FakeContainer(
            {
                "ingestion": AsyncMock(),
                "extraction": AsyncMock(),
                "grading": AsyncMock(),
                "draft_gen": AsyncMock(),
            }
        )
    )


class TestHandlerCanaryOrchestrator:
    def test_constructor_resolves_protocols_from_container(self) -> None:
        services = {
            "ingestion": AsyncMock(),
            "extraction": AsyncMock(),
            "grading": AsyncMock(),
            "draft_gen": AsyncMock(),
        }
        handler = HandlerCanaryOrchestrator(FakeContainer(services))

        assert handler._ingestion is services["ingestion"]  # noqa: SLF001
        assert handler._extraction is services["extraction"]  # noqa: SLF001
        assert handler._grading is services["grading"]  # noqa: SLF001
        assert handler._draft_gen is services["draft_gen"]  # noqa: SLF001

    def test_dry_run(self, tmp_path: Path, minimal_manifest: Path) -> None:
        handler = _stub_handler()
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
        handler = _stub_handler()
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
            FakeContainer(
                {
                    "ingestion": mock_ingest,
                    "extraction": AsyncMock(),
                    "grading": AsyncMock(),
                    "draft_gen": AsyncMock(),
                }
            )
        )
        handler._allow_external_providers = False  # noqa: SLF001

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
            FakeContainer(
                {
                    "ingestion": mock_ingest,
                    "extraction": mock_extract,
                    "grading": mock_grader,
                    "draft_gen": mock_draft,
                }
            )
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
        mocks = {
            "ingestion": AsyncMock(),
            "extraction": AsyncMock(),
            "grading": AsyncMock(),
            "draft_gen": AsyncMock(),
        }
        handler = HandlerCanaryOrchestrator.from_contract(
            contract, container=FakeContainer(mocks)
        )
        assert handler._max_concurrent_extractions == 8  # noqa: SLF001
        assert handler._grader_model_key == "deepseek-r1"  # noqa: SLF001
        assert handler._allow_external_providers is True  # noqa: SLF001

    def test_from_contract_defaults(self) -> None:
        mocks = {
            "ingestion": AsyncMock(),
            "extraction": AsyncMock(),
            "grading": AsyncMock(),
            "draft_gen": AsyncMock(),
        }
        handler = HandlerCanaryOrchestrator.from_contract(
            {}, container=FakeContainer(mocks)
        )
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


class _FakeBusMessage:
    def __init__(self, value: bytes) -> None:
        self.value = value


class _FakeRequestResponseBus:
    def __init__(self, response_payload: dict[str, object]) -> None:
        self.response_payload = response_payload
        self.published: list[tuple[str, dict[str, object]]] = []
        self.subscriptions: dict[str, Callable[[object], Awaitable[None]]] = {}

    async def subscribe(
        self,
        topic: str,
        node_identity: object | None = None,
        on_message: object | None = None,
        **kwargs: object,
    ) -> object:
        if not callable(on_message):
            raise TypeError("on_message callback is required")
        self.subscriptions[topic] = on_message

        async def unsubscribe() -> None:
            self.subscriptions.pop(topic, None)

        return unsubscribe

    async def publish(
        self,
        topic: str,
        key: bytes | None,
        value: bytes,
        headers: object = None,
    ) -> None:
        envelope = json.loads(value.decode("utf-8"))
        self.published.append((topic, envelope))
        completed_topic = "onex.evt.omnimarket.adr-document-ingestion-completed.v1"
        on_message = self.subscriptions[completed_topic]
        response = {
            "correlation_id": envelope["correlation_id"],
            "payload": self.response_payload,
        }
        await on_message(_FakeBusMessage(json.dumps(response).encode("utf-8")))


class TestAdrBusProtocolAdapters:
    def test_ingestion_adapter_resolves_bus_from_container_and_awaits_response(
        self,
    ) -> None:
        from omnimarket.adapters.adr import AdapterBusAdrIngestion

        bus = _FakeRequestResponseBus(
            {
                "documents": [
                    {
                        "source_path": "docs/adr.md",
                        "repo_name": "omnimarket",
                        "git_sha": None,
                        "author": None,
                        "created_at": None,
                        "updated_at": None,
                        "file_size_bytes": 12,
                        "source_content_sha256": "abc123",
                    }
                ]
            }
        )
        adapter = AdapterBusAdrIngestion(FakeContainer({"event_bus": bus}))

        result = asyncio.get_event_loop().run_until_complete(
            adapter.ingest(["/tmp/source"])
        )

        assert result.root_paths == ["/tmp/source"]
        assert result.documents[0].source_path == "docs/adr.md"
        assert bus.published[0][0] == (
            "onex.cmd.omnimarket.adr-document-ingestion-requested.v1"
        )
        payload = bus.published[0][1]["payload"]
        assert isinstance(payload, dict)
        assert payload["root_paths"] == ["/tmp/source"]
