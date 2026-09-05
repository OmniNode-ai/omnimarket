# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Bus-backed adapters for the ADR canary orchestrator protocols.

These adapters are the boundary between the orchestrator-owned shared ADR
protocol models and each sub-node's private command/result contract. They own
no bus lifecycle; the event bus is resolved from the DI container.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, TypeVar, cast
from uuid import uuid4

import yaml
from omnibase_core.errors.error_service_resolution import ServiceResolutionError
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_core.protocols.event_bus.protocol_event_bus_publisher import (
    ProtocolEventBusPublisher,
)
from pydantic import BaseModel

from omnimarket.models.adr import (
    ModelAdrDocumentRef,
    ModelAdrExtractionSummary,
    ModelAdrGradingScores,
    ModelAdrIngestionResult,
    ModelAdrSegmentationResult,
)
from omnimarket.models.adr import (
    ModelAdrDocumentSegment as ModelSharedAdrDocumentSegment,
)
from omnimarket.nodes.node_adr_decision_extraction_llm_effect.models.model_extraction_request import (
    ModelDocumentSegment as ModelExtractionDocumentSegment,
)
from omnimarket.nodes.node_adr_decision_extraction_llm_effect.models.model_extraction_request import (
    ModelExtractionRequest,
)
from omnimarket.nodes.node_adr_decision_extraction_llm_effect.models.model_extraction_result import (
    ModelExtractionResult,
)
from omnimarket.nodes.node_adr_document_ingestion_effect.models.model_ingestion_request import (
    ModelIngestionRequest,
)
from omnimarket.nodes.node_adr_document_ingestion_effect.models.model_ingestion_result import (
    ModelIngestionResult,
)
from omnimarket.nodes.node_adr_draft_generation_compute.models.model_decision_extraction import (
    EnumDecisionType as EnumDraftDecisionType,
)
from omnimarket.nodes.node_adr_draft_generation_compute.models.model_decision_extraction import (
    ModelDecisionExtraction as ModelDraftDecisionExtraction,
)
from omnimarket.nodes.node_adr_draft_generation_compute.models.model_decision_extraction import (
    ModelExtractionProvenance,
)
from omnimarket.nodes.node_adr_draft_generation_compute.models.model_generation_request import (
    ModelADRGenerationRequest,
)
from omnimarket.nodes.node_adr_draft_generation_compute.models.model_generation_result import (
    EnumGenerationStatus,
    ModelADRGenerationResult,
)
from omnimarket.nodes.node_adr_extraction_grader_llm_effect.models.model_grading_request import (
    ModelGradingRequest,
)
from omnimarket.nodes.node_adr_extraction_grader_llm_effect.models.model_grading_result import (
    ModelGradingResult,
)
from omnimarket.nodes.node_adr_segmentation_llm_effect.models.model_segmentation_request import (
    ModelSegmentationRequest,
)
from omnimarket.nodes.node_adr_segmentation_llm_effect.models.model_segmentation_result import (
    ModelSegmentationResult,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_NODES_DIR = _REPO_ROOT / "nodes"

T = TypeVar("T", bound=BaseModel)


class ProtocolAdrRequestResponseBus(Protocol):
    """Structural request/response capability; never a DI lookup key."""

    async def publish(
        self,
        topic: str,
        key: bytes | None,
        value: bytes,
        headers: object = None,
    ) -> None: ...

    async def subscribe(
        self,
        topic: str,
        node_identity: object | None = None,
        on_message: object | None = None,
        **kwargs: object,
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class ModelAdrBusProtocolAdapters:
    ingestion: AdapterBusAdrIngestion
    segmentation: HandlerBusAdrSegmentation
    extraction: AdapterBusAdrExtraction
    grading: AdapterBusAdrGrading
    draft_gen: AdapterBusAdrDraftGen


@dataclass(frozen=True, slots=True)
class _TopicPair:
    request: str
    completed: str
    timeout_seconds: float


class _BusRequestResponseClient:
    def __init__(
        self,
        event_bus: ProtocolAdrRequestResponseBus,
        *,
        request_topic: str,
        completed_topic: str,
        response_timeout_seconds: float,
        source_tool: str,
    ) -> None:
        self._event_bus = event_bus
        self._request_topic = request_topic
        self._completed_topic = completed_topic
        self._response_timeout_seconds = response_timeout_seconds
        self._source_tool = source_tool

    async def request(self, payload: BaseModel, response_model: type[T]) -> T:
        correlation_id = str(uuid4())
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1)

        async def on_message(message: object) -> None:
            raw_value = getattr(message, "value", message)
            parsed = _decode_message_value(raw_value)
            if parsed is None:
                return
            if _message_correlation_id(parsed) != correlation_id:
                return
            response_payload = _message_payload(parsed)
            if isinstance(response_payload, dict):
                await q.put(response_payload)

        unsubscribe = await _subscribe(
            self._event_bus,
            self._completed_topic,
            on_message,
            group_id=f"adr-bus-adapter-{correlation_id}",
        )
        try:
            await _publish_enveloped(
                self._event_bus,
                topic=self._request_topic,
                payload=payload,
                correlation_id=correlation_id,
                source_tool=self._source_tool,
            )
            response = await asyncio.wait_for(
                q.get(), timeout=self._response_timeout_seconds
            )
            return response_model.model_validate(response)
        finally:
            await _unsubscribe(unsubscribe)


class AdapterBusAdrIngestion:
    """ProtocolAdrIngestion implementation over the event bus."""

    def __init__(self, container: object) -> None:
        self._client = _client(
            _resolve_event_bus(container),
            _topic_pair(
                "node_adr_document_ingestion_effect",
                request_fragment="requested",
                completed_fragment="completed",
            ),
            source_tool="AdapterBusAdrIngestion",
        )

    async def ingest(
        self, *, root_paths: list[str], workspace_root: str
    ) -> ModelAdrIngestionResult:
        result = await self._client.request(
            ModelIngestionRequest(
                root_paths=root_paths,
                workspace_root=workspace_root,
            ),
            ModelIngestionResult,
        )
        return ModelAdrIngestionResult(
            root_paths=[workspace_root],
            documents=[
                ModelAdrDocumentRef(
                    source_path=doc.source_path,
                    repo_name=doc.repo_name,
                    file_size_bytes=doc.file_size_bytes,
                    source_content_sha256=doc.source_content_sha256,
                )
                for doc in result.documents
            ],
        )


class HandlerBusAdrSegmentation:
    """Resolve ingested documents through the ADR segmentation effect over the bus."""

    def __init__(self, container: object) -> None:
        self._client = _client(
            _resolve_event_bus(container),
            _topic_pair(
                "node_adr_segmentation_llm_effect",
                request_fragment="requested",
                completed_fragment="completed",
            ),
            source_tool="HandlerBusAdrSegmentation",
        )

    async def segment(
        self,
        *,
        ingestion: ModelAdrIngestionResult,
        correlation_id: str,
    ) -> ModelAdrSegmentationResult:
        """Segment each ingested document once for a canary entry.

        The orchestrator fans the resulting immutable shared segments out to each
        extraction model. A missing, changed, or failed source is a typed entry
        failure; silently omitting it would make downstream extraction claims
        incomplete provenance.
        """
        segments: list[ModelSharedAdrDocumentSegment] = []
        for document in ingestion.documents:
            content = _read_document_content(ingestion.root_paths, document.source_path)
            if content is None:
                return ModelAdrSegmentationResult(
                    success=False,
                    error_code="SOURCE_DOCUMENT_UNREADABLE",
                    error_message=(
                        "Could not reopen ingested document through its canonical "
                        f"workspace root: {document.source_path}"
                    ),
                )
            content_sha256 = hashlib.sha256(content.encode()).hexdigest()
            if content_sha256 != document.source_content_sha256:
                return ModelAdrSegmentationResult(
                    success=False,
                    error_code="SOURCE_DOCUMENT_HASH_MISMATCH",
                    error_message=(
                        f"Ingested document changed before segmentation: "
                        f"{document.source_path}"
                    ),
                )
            result = await self._client.request(
                ModelSegmentationRequest(
                    source_path=document.source_path,
                    source_content=content,
                    source_content_sha256=document.source_content_sha256,
                    correlation_id=correlation_id,
                ),
                ModelSegmentationResult,
            )
            if not result.success:
                error = result.error_message or "segmentation returned failure"
                return ModelAdrSegmentationResult(
                    success=False,
                    error_code=result.error_code or "SEGMENTATION_FAILED",
                    error_message=(
                        f"Segmentation failed for {document.source_path}: {error}"
                    ),
                )
            segments.extend(
                ModelSharedAdrDocumentSegment(
                    segment_id=segment.segment_id,
                    source_path=segment.source_path,
                    source_content_sha256=segment.source_content_sha256,
                    start_line=segment.start_line,
                    end_line=segment.end_line,
                    segment_type=segment.segment_type.value,
                    content=segment.content,
                    segment_content_sha256=segment.segment_content_sha256,
                    confidence=segment.confidence,
                )
                for segment in result.segments
            )
        return ModelAdrSegmentationResult(success=True, segments=tuple(segments))


class AdapterBusAdrExtraction:
    """ProtocolAdrExtraction implementation over the event bus."""

    def __init__(self, container: object) -> None:
        self._client = _client(
            _resolve_event_bus(container),
            _topic_pair(
                "node_adr_decision_extraction_llm_effect",
                request_fragment="requested",
                completed_fragment="completed",
            ),
            source_tool="AdapterBusAdrExtraction",
        )

    async def extract(
        self,
        *,
        segments: tuple[ModelSharedAdrDocumentSegment, ...],
        model_key: str,
        model_id: str,
        correlation_id: str,
    ) -> ModelAdrExtractionSummary:
        if not segments:
            return ModelAdrExtractionSummary(
                success=False,
                model_key=model_key,
                error_code="NO_DOCUMENT_SEGMENTS",
                error_message="No readable source documents were available for extraction.",
            )

        request = ModelExtractionRequest(
            segments=[
                ModelExtractionDocumentSegment(
                    segment_id=segment.segment_id,
                    source_path=segment.source_path,
                    start_line=segment.start_line,
                    end_line=segment.end_line,
                    segment_type=segment.segment_type,
                    content=segment.content,
                    confidence=segment.confidence,
                )
                for segment in segments
            ],
            model_key=model_key,
            model_config_overrides={"model_id": model_id},
            correlation_id=str(uuid4()),
            source_path=segments[0].source_path,
        )
        result = await self._client.request(request, ModelExtractionResult)
        raw_extractions = [
            extraction.model_dump(mode="json") for extraction in result.extractions
        ]
        first_extraction_json = (
            json.dumps(raw_extractions[0], sort_keys=True) if raw_extractions else ""
        )
        return ModelAdrExtractionSummary(
            success=result.success,
            model_key=result.model_key,
            model_id=model_id,
            extraction_count=len(result.extractions),
            extractions_raw=cast("list[dict[str, object]]", raw_extractions),
            first_extraction_json=first_extraction_json,
            error_code=result.error_code,
            error_message=result.error_message,
        )


class AdapterBusAdrGrading:
    """ProtocolAdrGrading implementation over the event bus."""

    def __init__(self, container: object) -> None:
        self._client = _client(
            _resolve_event_bus(container),
            _topic_pair(
                "node_adr_extraction_grader_llm_effect",
                request_fragment="requested",
                completed_fragment="completed",
            ),
            source_tool="AdapterBusAdrGrading",
        )

    async def grade(
        self,
        *,
        ground_truth_adr: str,
        extraction: ModelAdrExtractionSummary,
        source_summary: str,
        correlation_id: str,
    ) -> ModelAdrGradingScores:
        result = await self._client.request(
            ModelGradingRequest(
                ground_truth_adr=ground_truth_adr,
                extraction_output=extraction.extractions_raw,
                source_document=source_summary,
                correlation_id=str(uuid4()),
                model_key_under_test=extraction.model_key,
            ),
            ModelGradingResult,
        )
        return ModelAdrGradingScores(
            success=result.success,
            recall=result.recall,
            precision=result.precision,
            fidelity=result.fidelity,
            format_compliance=result.format_compliance,
            error_code=result.error_code,
            error_message=result.error_message,
            latency_ms=result.llm_call_evidence.latency_ms
            if result.llm_call_evidence is not None
            else 0,
        )


class AdapterBusAdrDraftGen:
    """ProtocolAdrDraftGen implementation over the event bus."""

    def __init__(self, container: object) -> None:
        self._client = _client(
            _resolve_event_bus(container),
            _topic_pair(
                "node_adr_draft_generation_compute",
                request_fragment="start",
                completed_fragment="completed",
            ),
            source_tool="AdapterBusAdrDraftGen",
        )

    async def generate(
        self,
        *,
        extraction: ModelAdrExtractionSummary,
        run_id: str,
    ) -> str:
        draft_extraction = _draft_extraction_from_summary(extraction, run_id=run_id)
        result = await self._client.request(
            ModelADRGenerationRequest(extraction=draft_extraction, run_id=run_id),
            ModelADRGenerationResult,
        )
        if result.status != EnumGenerationStatus.OK:
            return ""
        return result.markdown


def build_adr_bus_protocol_adapters(container: object) -> ModelAdrBusProtocolAdapters:
    """Build all ADR canary protocol adapters from node contract topics."""
    return ModelAdrBusProtocolAdapters(
        ingestion=AdapterBusAdrIngestion(container),
        segmentation=HandlerBusAdrSegmentation(container),
        extraction=AdapterBusAdrExtraction(container),
        grading=AdapterBusAdrGrading(container),
        draft_gen=AdapterBusAdrDraftGen(container),
    )


def _client(
    event_bus: ProtocolAdrRequestResponseBus,
    topics: _TopicPair,
    *,
    source_tool: str,
) -> _BusRequestResponseClient:
    return _BusRequestResponseClient(
        event_bus,
        request_topic=topics.request,
        completed_topic=topics.completed,
        response_timeout_seconds=topics.timeout_seconds,
        source_tool=source_tool,
    )


def _resolve_event_bus(container: object) -> ProtocolAdrRequestResponseBus:
    """Resolve the kernel-registered bus with request/response capabilities."""
    event_bus = _resolve_container_value(
        container,
        protocol_type=ProtocolEventBusPublisher,
    )
    if event_bus is None and isinstance(container, dict):
        event_bus = container.get("event_bus")
    if event_bus is None or not _supports_request_response(event_bus):
        raise TypeError(
            "ADR bus protocol adapters require the runtime-registered "
            "ProtocolEventBusPublisher to structurally support subscribe."
        )
    return cast("ProtocolAdrRequestResponseBus", event_bus)


def _resolve_container_value(
    container: object,
    *,
    protocol_type: type[object],
) -> object | None:
    if isinstance(container, dict):
        return container.get(protocol_type)

    for method_name in (
        "get_service",
        "get_service_sync",
        "get_service_optional",
    ):
        method = getattr(container, method_name, None)
        if not callable(method):
            continue
        resolved = _call_optional(method, protocol_type)
        if resolved is not None:
            return resolved

    for method_name in ("resolve", "get"):
        method = getattr(container, method_name, None)
        if not callable(method):
            continue
        resolved = _call_optional(method, protocol_type)
        if resolved is not None:
            return resolved

    return None


def _call_optional(
    method: Callable[..., object],
    *args: object,
    **kwargs: object,
) -> object | None:
    try:
        return method(*args, **kwargs)
    except ServiceResolutionError:
        return None


def _supports_request_response(candidate: object) -> bool:
    """Check the subscriber capability that request/response needs."""
    return callable(getattr(candidate, "publish", None)) and callable(
        getattr(candidate, "subscribe", None)
    )


def _topic_pair(
    node_name: str,
    *,
    request_fragment: str,
    completed_fragment: str,
) -> _TopicPair:
    contract_path = _NODES_DIR / node_name / "contract.yaml"
    raw = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{contract_path} did not load as a mapping")
    event_bus = raw.get("event_bus")
    if not isinstance(event_bus, dict):
        raise ValueError(f"{contract_path} has no event_bus mapping")
    subscribe_topics = _string_list(event_bus.get("subscribe_topics"))
    publish_topics = _string_list(event_bus.get("publish_topics"))
    request_topic = _single_topic(subscribe_topics, request_fragment, contract_path)
    completed_topic = _single_topic(publish_topics, completed_fragment, contract_path)
    timeout_ms = 300_000
    descriptor = raw.get("descriptor")
    if isinstance(descriptor, dict):
        raw_timeout = descriptor.get("timeout_ms")
        if isinstance(raw_timeout, int | float) and raw_timeout > 0:
            timeout_ms = int(raw_timeout)
    performance = raw.get("performance")
    if isinstance(performance, dict):
        raw_timeout = performance.get("max_response_time_ms")
        if isinstance(raw_timeout, int | float) and raw_timeout > 0:
            timeout_ms = int(raw_timeout)
    return _TopicPair(
        request=request_topic,
        completed=completed_topic,
        timeout_seconds=timeout_ms / 1000,
    )


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _single_topic(topics: list[str], fragment: str, contract_path: Path) -> str:
    matches = [topic for topic in topics if fragment in topic]
    if len(matches) != 1:
        raise ValueError(
            f"{contract_path} expected one topic containing {fragment!r}; "
            f"found {len(matches)}"
        )
    return matches[0]


def _read_document_content(root_paths: list[str], source_path: str) -> str | None:
    relative_source_path = Path(source_path)
    if relative_source_path.is_absolute():
        return None
    for root in root_paths:
        root_path = Path(root).resolve()
        candidate = (root_path / relative_source_path).resolve()
        try:
            candidate.relative_to(root_path)
        except ValueError:
            continue
        if candidate.is_file():
            # Preserve original line endings so re-encoding this UTF-8 content
            # verifies the byte-level hash recorded by ingestion.
            try:
                with candidate.open(encoding="utf-8", newline="") as source_file:
                    return source_file.read()
            except (OSError, UnicodeError):
                return None
    return None


def _draft_extraction_from_summary(
    extraction: ModelAdrExtractionSummary,
    *,
    run_id: str,
) -> ModelDraftDecisionExtraction:
    raw = _first_extraction_dict(extraction)
    statement = str(raw.get("statement") or raw.get("title") or "Untitled decision")
    rationale = raw.get("rationale")
    evidence_quotes = raw.get("evidence_quotes")
    rationale_bullets: list[str] = []
    if rationale:
        rationale_bullets.append(str(rationale))
    if isinstance(evidence_quotes, list):
        rationale_bullets.extend(str(item) for item in evidence_quotes)
    source_paths = raw.get("source_segment_ids")
    return ModelDraftDecisionExtraction(
        extraction_id=str(
            raw.get("extraction_id") or hashlib.sha256(statement.encode()).hexdigest()
        ),
        title=statement,
        decision_type=_draft_decision_type(raw.get("decision_type")),
        rationale_bullets=rationale_bullets,
        consequences=[],
        alternatives_considered=[],
        model_id=str(
            extraction.model_id
            or raw.get("extraction_model_id")
            or extraction.model_key
        ),
        confidence=_float_or_zero(raw.get("confidence")),
        provenance=ModelExtractionProvenance(
            source_doc_paths=[str(item) for item in source_paths]
            if isinstance(source_paths, list)
            else [],
            prompt_template_id=str(raw.get("prompt_template_id") or ""),
            prompt_template_version=str(raw.get("prompt_template_version") or ""),
            pipeline_version=(
                extraction.pipeline_version or "adr-canary-bus-adapter-v1"
            ),
            timestamp=datetime.now(UTC).isoformat(),
        ),
        canary_run_id=run_id,
    )


def _first_extraction_dict(extraction: ModelAdrExtractionSummary) -> dict[str, object]:
    if extraction.extractions_raw:
        return dict(extraction.extractions_raw[0])
    if extraction.first_extraction_json:
        try:
            raw = json.loads(extraction.first_extraction_json)
        except json.JSONDecodeError:
            return {}
        if isinstance(raw, dict):
            return cast("dict[str, object]", raw)
    return {}


def _float_or_zero(value: object) -> float:
    if isinstance(value, str | int | float):
        return float(value)
    return 0.0


def _draft_decision_type(value: object) -> EnumDraftDecisionType:
    raw = str(value or "").lower()
    if "security" in raw:
        return EnumDraftDecisionType.SECURITY
    if "data" in raw:
        return EnumDraftDecisionType.DATA
    if "integration" in raw:
        return EnumDraftDecisionType.INTEGRATION
    if "process" in raw or "operational" in raw:
        return EnumDraftDecisionType.PROCESS
    if "technology" in raw:
        return EnumDraftDecisionType.TECHNOLOGY
    return EnumDraftDecisionType.ARCHITECTURE


async def _publish_enveloped(
    event_bus: ProtocolAdrRequestResponseBus,
    *,
    topic: str,
    payload: BaseModel,
    correlation_id: str,
    source_tool: str,
) -> None:
    envelope: ModelEventEnvelope[BaseModel] = ModelEventEnvelope(
        payload=payload,
        correlation_id=correlation_id,
        envelope_timestamp=datetime.now(UTC),
        event_type=topic,
        source_tool=source_tool,
    )
    publish_envelope = getattr(event_bus, "publish_envelope", None)
    if callable(publish_envelope):
        await publish_envelope(envelope=envelope, topic=topic)
        return
    await event_bus.publish(
        topic,
        key=correlation_id.encode(),
        value=envelope.model_dump_json(exclude_none=True).encode("utf-8"),
    )


async def _subscribe(
    event_bus: ProtocolAdrRequestResponseBus,
    topic: str,
    on_message: Callable[[object], Awaitable[None]],
    *,
    group_id: str,
) -> object:
    try:
        return await event_bus.subscribe(
            topic=topic,
            node_identity=None,
            on_message=on_message,
            group_id=group_id,
        )
    except TypeError:
        return await event_bus.subscribe(
            topic=topic,
            on_message=on_message,
            group_id=group_id,
        )


async def _unsubscribe(unsubscribe: object) -> None:
    if not callable(unsubscribe):
        return
    result = unsubscribe()
    if asyncio.iscoroutine(result):
        await result


def _decode_message_value(raw_value: object) -> dict[str, Any] | None:
    if isinstance(raw_value, bytes):
        text = raw_value.decode("utf-8")
    elif isinstance(raw_value, str):
        text = raw_value
    elif isinstance(raw_value, dict):
        return cast("dict[str, Any]", raw_value)
    else:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _message_payload(message: dict[str, Any]) -> object:
    payload = message.get("payload", message)
    return payload


def _message_correlation_id(message: dict[str, Any]) -> str | None:
    candidate = message.get("correlation_id")
    if candidate is not None:
        return str(candidate)
    payload = _message_payload(message)
    if isinstance(payload, dict):
        candidate = payload.get("correlation_id")
        if candidate is not None:
            return str(candidate)
    return None
