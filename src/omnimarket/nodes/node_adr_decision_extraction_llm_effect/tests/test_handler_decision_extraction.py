# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Tests for HandlerDecisionExtraction."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_adr_decision_extraction_llm_effect.handlers.handler_decision_extraction import (
    HandlerDecisionExtraction,
    _compute_extraction_id,
    _parse_extractions,
    _validate_item,
)
from omnimarket.nodes.node_adr_decision_extraction_llm_effect.models.model_extraction_request import (
    ModelDocumentSegment,
    ModelExtractionRequest,
)
from omnimarket.nodes.node_adr_decision_extraction_llm_effect.models.model_extraction_result import (
    EnumDecisionType,
    ModelDecisionExtraction,
    ModelExtractionResult,
    ModelLLMCallEvidence,
)
from omnimarket.nodes.node_hostile_reviewer.handlers.adapter_inference_bridge import (
    ModelInferenceAdapter,
)

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

_SEG_1 = ModelDocumentSegment(
    segment_id="seg-aaa",
    source_path="docs/adr-001.md",
    start_line=1,
    end_line=5,
    segment_type="decision",
    content="We decided to use PostgreSQL as the primary database.",
    confidence=0.95,
)

_SEG_2 = ModelDocumentSegment(
    segment_id="seg-bbb",
    source_path="docs/adr-001.md",
    start_line=6,
    end_line=10,
    segment_type="doctrine_formation",
    content="All services must use connection pooling. Never open raw connections.",
    confidence=0.9,
)

_SEG_3 = ModelDocumentSegment(
    segment_id="seg-ccc",
    source_path="docs/adr-001.md",
    start_line=11,
    end_line=15,
    segment_type="failure_analysis",
    content="We burned ourselves in Q3 when Redis OOM'd under load — learned to cap key TTLs.",
    confidence=0.85,
)

_SEG_PIVOT = ModelDocumentSegment(
    segment_id="seg-ddd",
    source_path="docs/adr-002.md",
    start_line=1,
    end_line=4,
    segment_type="decision",
    content="We replaced Celery with Kafka after message loss incidents.",
    confidence=0.88,
)

_SEG_REJECTED = ModelDocumentSegment(
    segment_id="seg-eee",
    source_path="docs/adr-003.md",
    start_line=1,
    end_line=3,
    segment_type="non_decision",
    content="We considered Redis Streams but abandoned the approach after testing.",
    confidence=0.8,
)

_SEG_SUPERSESSION = ModelDocumentSegment(
    segment_id="seg-fff",
    source_path="docs/adr-004.md",
    start_line=1,
    end_line=3,
    segment_type="decision",
    content="This ADR supersedes ADR-003. The old approach is now deprecated.",
    confidence=0.92,
)


def _make_request(
    segments: list[ModelDocumentSegment] | None = None,
    model_key: str = "qwen3-coder",
    **overrides: object,
) -> ModelExtractionRequest:
    defaults: dict[str, object] = {
        "segments": segments or [_SEG_1, _SEG_2],
        "model_key": model_key,
        "correlation_id": "corr-test-001",
        "source_path": "docs/adr-001.md",
    }
    defaults.update(overrides)
    return ModelExtractionRequest(**defaults)


_SENTINEL: list[str] = []


def _extraction_payload(
    decision_type: str = "architecture_decision",
    statement: str = "Use PostgreSQL as primary database.",
    source_segment_ids: list[str] | None = _SENTINEL,  # type: ignore[assignment]
    confidence: float = 0.9,
    **extra: object,
) -> dict[str, object]:
    seg_ids = ["seg-aaa"] if source_segment_ids is _SENTINEL else source_segment_ids
    return {
        "decision_type": decision_type,
        "statement": statement,
        "rationale": "Reliability and team familiarity",
        "source_segment_ids": seg_ids,
        "evidence_quotes": ["We decided to use PostgreSQL"],
        "confidence": confidence,
        **extra,
    }


def _good_response(items: list[dict[str, object]] | None = None) -> str:
    if items is None:
        items = [_extraction_payload()]
    return json.dumps(items)


class _MockBridge(ModelInferenceAdapter):
    """Controllable mock inference bridge."""

    def __init__(self, responses: list[str | Exception]) -> None:
        self._responses = list(responses)
        self._call_count = 0

    async def infer(
        self,
        model_key: str,
        system_prompt: str,
        user_prompt: str,
        timeout_seconds: float,
    ) -> str:
        response = self._responses[min(self._call_count, len(self._responses) - 1)]
        self._call_count += 1
        if isinstance(response, Exception):
            raise response
        return response


# ---------------------------------------------------------------------------
# _parse_extractions unit tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_parse_extractions_valid_array() -> None:
    raw = json.dumps([_extraction_payload()])
    result = _parse_extractions(raw)
    assert result is not None
    assert len(result) == 1
    assert result[0]["decision_type"] == "architecture_decision"


@pytest.mark.unit
def test_parse_extractions_empty_array() -> None:
    assert _parse_extractions("[]") == []


@pytest.mark.unit
def test_parse_extractions_strips_markdown_fence() -> None:
    raw = "```json\n" + json.dumps([_extraction_payload()]) + "\n```"
    result = _parse_extractions(raw)
    assert result is not None
    assert len(result) == 1


@pytest.mark.unit
def test_parse_extractions_invalid_json_returns_none() -> None:
    assert _parse_extractions("not json") is None


@pytest.mark.unit
def test_parse_extractions_object_not_array_returns_none() -> None:
    assert _parse_extractions(json.dumps({"key": "value"})) is None


@pytest.mark.unit
def test_parse_extractions_empty_string_returns_none() -> None:
    assert _parse_extractions("") is None


# ---------------------------------------------------------------------------
# _validate_item unit tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_validate_item_valid() -> None:
    assert _validate_item(_extraction_payload()) is True


@pytest.mark.unit
def test_validate_item_missing_required_field() -> None:
    item = _extraction_payload()
    del item["decision_type"]  # type: ignore[misc]
    assert _validate_item(item) is False


@pytest.mark.unit
def test_validate_item_invalid_decision_type() -> None:
    item = _extraction_payload(decision_type="unknown_type")
    assert _validate_item(item) is False


@pytest.mark.unit
def test_validate_item_empty_segment_ids() -> None:
    item = _extraction_payload(source_segment_ids=[])
    assert _validate_item(item) is False


@pytest.mark.unit
def test_validate_item_out_of_range_confidence() -> None:
    item = _extraction_payload(confidence=1.5)
    assert _validate_item(item) is False


@pytest.mark.unit
def test_validate_item_boolean_confidence_rejected() -> None:
    item = _extraction_payload()
    item["confidence"] = True  # type: ignore[assignment]
    assert _validate_item(item) is False


@pytest.mark.unit
def test_validate_item_not_a_dict() -> None:
    assert _validate_item("not a dict") is False


# ---------------------------------------------------------------------------
# _compute_extraction_id determinism tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_compute_extraction_id_is_deterministic() -> None:
    eid = _compute_extraction_id(
        model_id="model-x",
        source_segment_ids=["seg-aaa", "seg-bbb"],
        segment_content_hashes=["hash1", "hash2"],
    )
    eid2 = _compute_extraction_id(
        model_id="model-x",
        source_segment_ids=["seg-aaa", "seg-bbb"],
        segment_content_hashes=["hash1", "hash2"],
    )
    assert eid == eid2


@pytest.mark.unit
def test_compute_extraction_id_order_independent_on_segment_ids() -> None:
    eid_forward = _compute_extraction_id(
        model_id="model-x",
        source_segment_ids=["seg-aaa", "seg-bbb"],
        segment_content_hashes=["hash1", "hash2"],
    )
    eid_reversed = _compute_extraction_id(
        model_id="model-x",
        source_segment_ids=["seg-bbb", "seg-aaa"],
        segment_content_hashes=["hash2", "hash1"],
    )
    assert eid_forward == eid_reversed


@pytest.mark.unit
def test_compute_extraction_id_differs_by_model_id() -> None:
    eid_a = _compute_extraction_id("model-a", ["seg-aaa"], ["hash1"])
    eid_b = _compute_extraction_id("model-b", ["seg-aaa"], ["hash1"])
    assert eid_a != eid_b


@pytest.mark.unit
def test_compute_extraction_id_differs_by_content_hash() -> None:
    eid_a = _compute_extraction_id("model-x", ["seg-aaa"], ["hash1"])
    eid_b = _compute_extraction_id("model-x", ["seg-aaa"], ["hash2"])
    assert eid_a != eid_b


# ---------------------------------------------------------------------------
# HandlerDecisionExtraction — happy path: each decision type
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_architecture_decision() -> None:
    payload = _extraction_payload(
        decision_type="architecture_decision",
        statement="Use PostgreSQL as primary database.",
        source_segment_ids=["seg-aaa"],
    )
    bridge = _MockBridge([json.dumps([payload])])
    handler = HandlerDecisionExtraction(inference_bridge=bridge)
    result = await handler.handle(_make_request(segments=[_SEG_1]))

    assert result.success is True
    assert len(result.extractions) == 1
    assert result.extractions[0].decision_type == EnumDecisionType.architecture_decision


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_architecture_pivot() -> None:
    payload = _extraction_payload(
        decision_type="architecture_pivot",
        statement="Replaced Celery with Kafka after message loss incidents.",
        source_segment_ids=["seg-ddd"],
    )
    bridge = _MockBridge([json.dumps([payload])])
    handler = HandlerDecisionExtraction(inference_bridge=bridge)
    result = await handler.handle(_make_request(segments=[_SEG_PIVOT]))

    assert result.success is True
    assert result.extractions[0].decision_type == EnumDecisionType.architecture_pivot


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_doctrine_formation() -> None:
    payload = _extraction_payload(
        decision_type="doctrine_formation",
        statement="All services must use connection pooling.",
        source_segment_ids=["seg-bbb"],
    )
    bridge = _MockBridge([json.dumps([payload])])
    handler = HandlerDecisionExtraction(inference_bridge=bridge)
    result = await handler.handle(_make_request(segments=[_SEG_2]))

    assert result.success is True
    assert result.extractions[0].decision_type == EnumDecisionType.doctrine_formation


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_operational_lesson() -> None:
    payload = _extraction_payload(
        decision_type="operational_lesson",
        statement="Redis OOM'd under load — cap key TTLs.",
        source_segment_ids=["seg-ccc"],
    )
    bridge = _MockBridge([json.dumps([payload])])
    handler = HandlerDecisionExtraction(inference_bridge=bridge)
    result = await handler.handle(_make_request(segments=[_SEG_3]))

    assert result.success is True
    assert result.extractions[0].decision_type == EnumDecisionType.operational_lesson


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_supersession() -> None:
    payload = _extraction_payload(
        decision_type="supersession",
        statement="This ADR supersedes ADR-003.",
        source_segment_ids=["seg-fff"],
    )
    bridge = _MockBridge([json.dumps([payload])])
    handler = HandlerDecisionExtraction(inference_bridge=bridge)
    result = await handler.handle(_make_request(segments=[_SEG_SUPERSESSION]))

    assert result.success is True
    assert result.extractions[0].decision_type == EnumDecisionType.supersession


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_rejected_approach() -> None:
    payload = _extraction_payload(
        decision_type="rejected_approach",
        statement="Redis Streams was abandoned after testing.",
        source_segment_ids=["seg-eee"],
    )
    bridge = _MockBridge([json.dumps([payload])])
    handler = HandlerDecisionExtraction(inference_bridge=bridge)
    result = await handler.handle(_make_request(segments=[_SEG_REJECTED]))

    assert result.success is True
    assert result.extractions[0].decision_type == EnumDecisionType.rejected_approach


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_empty_extractions_is_valid() -> None:
    bridge = _MockBridge(["[]"])
    handler = HandlerDecisionExtraction(inference_bridge=bridge)
    result = await handler.handle(_make_request())

    assert result.success is True
    assert result.extractions == []
    assert result.error_code is None


# ---------------------------------------------------------------------------
# JSON repair path
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_json_repair_succeeds_on_second_attempt() -> None:
    good = json.dumps([_extraction_payload(source_segment_ids=["seg-aaa"])])
    bridge = _MockBridge(["not valid json", good])
    handler = HandlerDecisionExtraction(inference_bridge=bridge)
    result = await handler.handle(_make_request(segments=[_SEG_1]))

    assert result.success is True
    assert len(result.extractions) == 1
    assert result.llm_call_evidence is not None
    assert result.llm_call_evidence.json_repair_attempted is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_json_repair_fails_on_second_attempt() -> None:
    bridge = _MockBridge(["not valid json", "still not valid json"])
    handler = HandlerDecisionExtraction(inference_bridge=bridge)
    result = await handler.handle(_make_request())

    assert result.success is False
    assert result.error_code == "EXTRACTION_PARSE_FAILED"
    assert result.llm_call_evidence is not None
    assert result.llm_call_evidence.json_repair_attempted is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_repair_llm_call_fails() -> None:
    bridge = _MockBridge(["not valid json", RuntimeError("timeout during repair")])
    handler = HandlerDecisionExtraction(inference_bridge=bridge)
    result = await handler.handle(_make_request())

    assert result.success is False
    assert result.error_code == "EXTRACTION_REPAIR_LLM_FAILED"
    assert result.retryable is True


# ---------------------------------------------------------------------------
# LLM failure path — must NOT return empty success
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_llm_failure_returns_success_false() -> None:
    bridge = _MockBridge([ConnectionError("connection refused")])
    handler = HandlerDecisionExtraction(inference_bridge=bridge)
    result = await handler.handle(_make_request())

    assert result.success is False
    assert result.error_code == "EXTRACTION_LLM_CALL_FAILED"
    assert result.error_message is not None
    assert "connection refused" in result.error_message
    assert result.retryable is True
    assert result.extractions == []
    assert result.llm_call_evidence is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_llm_timeout_returns_success_false() -> None:
    bridge = _MockBridge([TimeoutError("LLM call timed out")])
    handler = HandlerDecisionExtraction(inference_bridge=bridge)
    result = await handler.handle(_make_request())

    assert result.success is False
    assert result.error_code == "EXTRACTION_LLM_CALL_FAILED"
    assert result.retryable is True


# ---------------------------------------------------------------------------
# Evidence recording
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_records_evidence_on_success() -> None:
    bridge = _MockBridge([_good_response()])
    handler = HandlerDecisionExtraction(
        inference_bridge=bridge,
        prompt_template_id="adr_decision_extraction_v1",
        prompt_template_version="1.0.0",
    )
    result = await handler.handle(_make_request(segments=[_SEG_1]))

    assert result.success is True
    assert result.llm_call_evidence is not None
    ev = result.llm_call_evidence
    assert ev.prompt_template_id == "adr_decision_extraction_v1"
    assert ev.prompt_template_version == "1.0.0"
    assert ev.extraction_model_key == "qwen3-coder"
    assert ev.latency_ms >= 0
    assert ev.json_repair_attempted is False


# ---------------------------------------------------------------------------
# Deterministic extraction_id on result
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_extraction_id_is_deterministic() -> None:
    payload = _extraction_payload(source_segment_ids=["seg-aaa"])
    bridge1 = _MockBridge([json.dumps([payload])])
    bridge2 = _MockBridge([json.dumps([payload])])
    handler1 = HandlerDecisionExtraction(inference_bridge=bridge1)
    handler2 = HandlerDecisionExtraction(inference_bridge=bridge2)

    result1 = await handler1.handle(_make_request(segments=[_SEG_1]))
    result2 = await handler2.handle(_make_request(segments=[_SEG_1]))

    assert result1.success is True
    assert result2.success is True
    assert result1.extractions[0].extraction_id == result2.extractions[0].extraction_id


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_extraction_id_differs_by_model_key() -> None:
    payload = _extraction_payload(source_segment_ids=["seg-aaa"])
    bridge1 = _MockBridge([json.dumps([payload])])
    bridge2 = _MockBridge([json.dumps([payload])])
    handler = HandlerDecisionExtraction(inference_bridge=bridge1)
    handler2 = HandlerDecisionExtraction(inference_bridge=bridge2)

    result1 = await handler.handle(
        _make_request(model_key="qwen3-coder", segments=[_SEG_1])
    )
    result2 = await handler2.handle(
        _make_request(model_key="deepseek-r1", segments=[_SEG_1])
    )

    assert result1.success is True
    assert result2.success is True
    # model_key used as model_id in handler — IDs must differ
    assert result1.extractions[0].extraction_id != result2.extractions[0].extraction_id


# ---------------------------------------------------------------------------
# Invalid items in LLM output are skipped gracefully
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_invalid_items_are_skipped() -> None:
    items = [
        _extraction_payload(source_segment_ids=["seg-aaa"]),
        {
            "decision_type": "bad_type",
            "statement": "x",
            "source_segment_ids": ["seg-aaa"],
            "confidence": 0.8,
        },
        {"incomplete": True},
    ]
    bridge = _MockBridge([json.dumps(items)])
    handler = HandlerDecisionExtraction(inference_bridge=bridge)
    result = await handler.handle(_make_request(segments=[_SEG_1]))

    assert result.success is True
    assert len(result.extractions) == 1


# ---------------------------------------------------------------------------
# Model validation
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_extraction_request_requires_non_empty_segments() -> None:
    with pytest.raises(ValidationError):
        ModelExtractionRequest(
            segments=[],
            model_key="qwen3-coder",
            correlation_id="c1",
            source_path="docs/adr-001.md",
        )


@pytest.mark.unit
def test_extraction_result_is_frozen() -> None:
    result = ModelExtractionResult(
        correlation_id="c1",
        source_path="docs/adr-001.md",
        model_key="qwen3-coder",
        success=True,
    )
    with pytest.raises((ValidationError, TypeError)):
        result.success = False  # type: ignore[misc]


@pytest.mark.unit
def test_decision_extraction_model_is_frozen() -> None:
    eid = _compute_extraction_id("m", ["s1"], ["h1"])
    extraction = ModelDecisionExtraction(
        extraction_id=eid,
        decision_type=EnumDecisionType.architecture_decision,
        statement="Use PostgreSQL.",
        source_segment_ids=["s1"],
        extraction_model_id="model-x",
        prompt_template_id="adr_decision_extraction_v1",
        prompt_template_version="1.0.0",
        confidence=0.9,
    )
    with pytest.raises((ValidationError, TypeError)):
        extraction.statement = "mutated"  # type: ignore[misc]


@pytest.mark.unit
def test_llm_call_evidence_is_frozen() -> None:
    ev = ModelLLMCallEvidence(
        prompt_template_id="adr_decision_extraction_v1",
        prompt_template_version="1.0.0",
        extraction_model_key="qwen3-coder",
        extraction_model_id="model-x",
    )
    with pytest.raises((ValidationError, TypeError)):
        ev.latency_ms = 999  # type: ignore[misc]


@pytest.mark.unit
def test_enum_decision_type_all_values_valid() -> None:
    expected = {
        "architecture_decision",
        "architecture_pivot",
        "doctrine_formation",
        "operational_lesson",
        "supersession",
        "rejected_approach",
    }
    assert {e.value for e in EnumDecisionType} == expected
