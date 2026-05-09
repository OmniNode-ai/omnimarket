# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""ADR Canary real bus-backed golden chain.

This test proves the orchestrator uses bus-backed protocol adapters to reach
the real ADR sub-node handlers. LLM boundaries are deterministic fakes; the
bus, adapter translation, ingestion, extraction parsing, grading parsing, and
draft generation handlers are real.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol

import yaml
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from pydantic import BaseModel

from omnimarket.nodes.node_adr_canary_orchestrator.handlers.handler_canary_orchestrator import (
    HandlerCanaryOrchestrator,
)
from omnimarket.nodes.node_adr_canary_orchestrator.models.model_canary_request import (
    ModelCanaryCommandPayload,
)
from omnimarket.nodes.node_adr_decision_extraction_llm_effect.handlers.handler_decision_extraction import (
    HandlerDecisionExtraction,
)
from omnimarket.nodes.node_adr_decision_extraction_llm_effect.models.model_extraction_request import (
    ModelExtractionRequest,
)
from omnimarket.nodes.node_adr_document_ingestion_effect.handlers.handler_document_ingestion import (
    HandlerDocumentIngestion,
)
from omnimarket.nodes.node_adr_document_ingestion_effect.models.model_ingestion_request import (
    ModelIngestionRequest,
)
from omnimarket.nodes.node_adr_draft_generation_compute.handlers.handler_adr_generation import (
    HandlerADRGeneration,
)
from omnimarket.nodes.node_adr_draft_generation_compute.models.model_generation_request import (
    ModelADRGenerationRequest,
)
from omnimarket.nodes.node_adr_extraction_grader_llm_effect.handlers.handler_extraction_grader import (
    HandlerExtractionGrader,
)
from omnimarket.nodes.node_adr_extraction_grader_llm_effect.models.model_grading_request import (
    ModelGradingRequest,
)

_NODES_ROOT = Path("src/omnimarket/nodes")


class _DeterministicAdrInferenceBridge:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def infer(
        self,
        *,
        model_key: str,
        system_prompt: str,
        user_prompt: str,
        timeout_seconds: float,
    ) -> str:
        self.calls.append(model_key)
        if model_key == "opus":
            return json.dumps(
                {
                    "recall": 0.91,
                    "precision": 0.88,
                    "fidelity": 0.93,
                    "format_compliance": 1.0,
                    "rationale": "captured primary bus decision | no unrelated items | faithful wording | schema valid",
                }
            )

        segment_id = _first_segment_id(user_prompt)
        return json.dumps(
            [
                {
                    "decision_type": "architecture_decision",
                    "statement": "Use Kafka/Redpanda as the primary event bus",
                    "rationale": (
                        "Contracts own topic definitions and clients consume "
                        "materialized projections rather than owning truth."
                    ),
                    "source_segment_ids": [segment_id],
                    "evidence_quotes": [
                        "We decided to use Kafka/Redpanda as the primary event bus."
                    ],
                    "confidence": 0.93,
                }
            ]
        )


class _BusMessage(Protocol):
    value: bytes


def _first_segment_id(prompt: str) -> str:
    match = re.search(r"\[segment_id=([^ ]+)", prompt)
    if not match:
        raise AssertionError(f"segment_id missing from extraction prompt: {prompt}")
    return match.group(1)


def _node_topics(
    node_name: str,
    *,
    request_fragment: str,
    completed_fragment: str,
) -> tuple[str, str]:
    raw = yaml.safe_load((_NODES_ROOT / node_name / "contract.yaml").read_text())
    event_bus = raw["event_bus"]
    request_topic = _single_matching_topic(
        event_bus["subscribe_topics"], request_fragment
    )
    completed_topic = _single_matching_topic(
        event_bus["publish_topics"], completed_fragment
    )
    return request_topic, completed_topic


def _single_matching_topic(topics: list[str], fragment: str) -> str:
    matches = [topic for topic in topics if fragment in topic]
    assert len(matches) == 1
    return matches[0]


def _decode_envelope(message: _BusMessage) -> tuple[str, dict[str, Any]]:
    envelope = json.loads(message.value.decode("utf-8"))
    payload = envelope.get("payload")
    assert isinstance(payload, dict)
    return str(envelope["correlation_id"]), payload


async def _publish_result(
    bus: EventBusInmemory,
    *,
    topic: str,
    correlation_id: str,
    result: BaseModel,
) -> None:
    await bus.publish(
        topic,
        key=correlation_id.encode(),
        value=json.dumps(
            {
                "correlation_id": correlation_id,
                "payload": result.model_dump(mode="json"),
            }
        ).encode("utf-8"),
    )


async def _subscribe_async_handler(
    bus: EventBusInmemory,
    *,
    request_topic: str,
    completed_topic: str,
    request_model: type[BaseModel],
    handler: Callable[[BaseModel], Awaitable[BaseModel]],
    group_id: str,
) -> Callable[[], Awaitable[None]]:
    async def on_message(message: _BusMessage) -> None:
        correlation_id, payload = _decode_envelope(message)
        request = request_model.model_validate(payload)
        result = await handler(request)
        await _publish_result(
            bus,
            topic=completed_topic,
            correlation_id=correlation_id,
            result=result,
        )

    return await bus.subscribe(
        request_topic,
        on_message=on_message,
        group_id=group_id,
    )


async def _wire_adr_subnodes(
    bus: EventBusInmemory,
    inference_bridge: _DeterministicAdrInferenceBridge,
) -> list[Callable[[], Awaitable[None]]]:
    ingestion_request, ingestion_completed = _node_topics(
        "node_adr_document_ingestion_effect",
        request_fragment="requested",
        completed_fragment="completed",
    )
    extraction_request, extraction_completed = _node_topics(
        "node_adr_decision_extraction_llm_effect",
        request_fragment="requested",
        completed_fragment="completed",
    )
    grading_request, grading_completed = _node_topics(
        "node_adr_extraction_grader_llm_effect",
        request_fragment="requested",
        completed_fragment="completed",
    )
    draft_request, draft_completed = _node_topics(
        "node_adr_draft_generation_compute",
        request_fragment="start",
        completed_fragment="completed",
    )

    ingestion_handler = HandlerDocumentIngestion()
    extraction_handler = HandlerDecisionExtraction(inference_bridge=inference_bridge)
    grading_handler = HandlerExtractionGrader(inference_bridge=inference_bridge)
    draft_handler = HandlerADRGeneration()

    async def handle_ingestion(request: BaseModel) -> BaseModel:
        return await ingestion_handler.handle(
            request=ModelIngestionRequest.model_validate(request)
        )

    async def handle_extraction(request: BaseModel) -> BaseModel:
        return await extraction_handler.handle(
            ModelExtractionRequest.model_validate(request)
        )

    async def handle_grading(request: BaseModel) -> BaseModel:
        return await grading_handler.handle(ModelGradingRequest.model_validate(request))

    async def handle_draft(request: BaseModel) -> BaseModel:
        return draft_handler.handle(ModelADRGenerationRequest.model_validate(request))

    return [
        await _subscribe_async_handler(
            bus,
            request_topic=ingestion_request,
            completed_topic=ingestion_completed,
            request_model=ModelIngestionRequest,
            handler=handle_ingestion,
            group_id="test-adr-ingestion-real-handler",
        ),
        await _subscribe_async_handler(
            bus,
            request_topic=extraction_request,
            completed_topic=extraction_completed,
            request_model=ModelExtractionRequest,
            handler=handle_extraction,
            group_id="test-adr-extraction-real-handler",
        ),
        await _subscribe_async_handler(
            bus,
            request_topic=grading_request,
            completed_topic=grading_completed,
            request_model=ModelGradingRequest,
            handler=handle_grading,
            group_id="test-adr-grading-real-handler",
        ),
        await _subscribe_async_handler(
            bus,
            request_topic=draft_request,
            completed_topic=draft_completed,
            request_model=ModelADRGenerationRequest,
            handler=handle_draft,
            group_id="test-adr-draft-real-handler",
        ),
    ]


def _write_fixture_manifest(tmp_path: Path) -> tuple[Path, Path]:
    source_root = tmp_path / "fixture_repo"
    docs_dir = source_root / "docs"
    docs_dir.mkdir(parents=True)
    (docs_dir / "event-bus.md").write_text(
        "\n".join(
            [
                "# ADR: Kafka/Redpanda as Primary Event Bus",
                "",
                "We decided to use Kafka/Redpanda as the primary event bus.",
                "Contracts own topic definitions.",
                "Clients consume materialized projections rather than owning authoritative state.",
                "Rejected approach: clients writing their own state directly.",
            ]
        ),
        encoding="utf-8",
    )

    manifest_path = tmp_path / "manifest.yaml"
    manifest_path.write_text(
        yaml.safe_dump(
            {
                "entries": [
                    {
                        "id": "event-bus-adr",
                        "root_paths": [str(source_root)],
                        "ground_truth_adr": (
                            "# ADR: Kafka/Redpanda as Primary Event Bus\n\n"
                            "Use Kafka/Redpanda as the primary event bus; contracts "
                            "own topic definitions and projections are materialized."
                        ),
                        "models": [
                            {
                                "key": "deterministic-local",
                                "provider": "local",
                                "model_id": "fake-deterministic-adr-extractor",
                                "external": False,
                            }
                        ],
                    }
                ]
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return source_root, manifest_path


async def test_adr_canary_real_bus_backed_chain_reaches_all_subnodes(
    event_bus: EventBusInmemory,
    tmp_path: Path,
) -> None:
    source_root, manifest_path = _write_fixture_manifest(tmp_path)
    inference_bridge = _DeterministicAdrInferenceBridge()

    await event_bus.start()
    unsubscribers = await _wire_adr_subnodes(event_bus, inference_bridge)
    try:
        handler = HandlerCanaryOrchestrator({"event_bus": event_bus})

        report = await handler.handle(
            ModelCanaryCommandPayload(
                manifest_path=str(manifest_path),
                output_dir=str(tmp_path / "runs"),
                model_subset=["deterministic-local"],
                resume_run_id="OMN-10724-real-chain-proof",
            )
        )
    finally:
        for unsubscribe in unsubscribers:
            await unsubscribe()
        await event_bus.close()

    assert report.success is True
    assert report.entries_total == 1
    assert report.entries_completed == 1
    assert report.entries_failed == 0
    assert len(report.model_scores) == 1
    assert report.model_scores[0].model_key == "deterministic-local"
    assert report.model_scores[0].avg_recall == 0.91
    assert inference_bridge.calls == ["deterministic-local", "opus"]

    evidence_dir = Path(report.evidence_dir)
    evidence_json = evidence_dir / "event-bus-adr" / "deterministic-local.json"
    draft_markdown = evidence_dir / "event-bus-adr" / "deterministic-local_draft.md"
    scorecard = Path(report.scorecard_path)
    assert evidence_json.exists()
    assert draft_markdown.exists()
    assert scorecard.exists()

    evidence = json.loads(evidence_json.read_text(encoding="utf-8"))
    assert evidence["extraction_success"] is True
    assert evidence["grading_success"] is True
    assert evidence["draft_generated"] is True

    draft = draft_markdown.read_text(encoding="utf-8")
    assert "# ADR: Use Kafka/Redpanda as the primary event bus" in draft
    assert "adr-canary-bus-adapter-v1" in draft

    assert (source_root / "docs" / "event-bus.md").exists()
    for node_name, request_fragment, completed_fragment in [
        ("node_adr_document_ingestion_effect", "requested", "completed"),
        ("node_adr_decision_extraction_llm_effect", "requested", "completed"),
        ("node_adr_extraction_grader_llm_effect", "requested", "completed"),
        ("node_adr_draft_generation_compute", "start", "completed"),
    ]:
        request_topic, completed_topic = _node_topics(
            node_name,
            request_fragment=request_fragment,
            completed_fragment=completed_fragment,
        )
        assert len(await event_bus.get_event_history(topic=request_topic)) == 1
        assert len(await event_bus.get_event_history(topic=completed_topic)) == 1
