# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Tests for HandlerSegmentation."""

from __future__ import annotations

import hashlib
import json

import pytest

from omnimarket.nodes.node_adr_segmentation_llm_effect.handlers.handler_segmentation import (
    HandlerSegmentation,
    _build_user_prompt,
    _compute_segment_id,
    _parse_segments,
    _sha256,
)
from omnimarket.nodes.node_adr_segmentation_llm_effect.models.model_segmentation_request import (
    ModelSegmentationRequest,
)
from omnimarket.nodes.node_adr_segmentation_llm_effect.models.model_segmentation_result import (
    EnumSegmentType,
    ModelDocumentSegment,
    ModelLLMCallEvidence,
    ModelSegmentationResult,
)
from omnimarket.nodes.node_hostile_reviewer.handlers.adapter_inference_bridge import (
    ModelInferenceAdapter,
)

# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------

_SOURCE_CONTENT = """\
# ADR-001: Use PostgreSQL

## Status
Accepted

## Context
We evaluated several databases.

## Decision
Use PostgreSQL 15.

## Consequences
Team must train on PostgreSQL.
"""

_SOURCE_PATH = "docs/adr/adr-001-postgres.md"
_SOURCE_SHA = hashlib.sha256(_SOURCE_CONTENT.encode()).hexdigest()

_GOOD_SEGMENTS = [
    {
        "start_line": 1,
        "end_line": 1,
        "segment_type": "background",
        "content": "# ADR-001: Use PostgreSQL",
        "confidence": 0.9,
    },
    {
        "start_line": 3,
        "end_line": 4,
        "segment_type": "non_decision",
        "content": "## Status\nAccepted",
        "confidence": 0.85,
    },
    {
        "start_line": 6,
        "end_line": 7,
        "segment_type": "background",
        "content": "## Context\nWe evaluated several databases.",
        "confidence": 0.8,
    },
    {
        "start_line": 9,
        "end_line": 10,
        "segment_type": "decision",
        "content": "## Decision\nUse PostgreSQL 15.",
        "confidence": 0.95,
    },
    {
        "start_line": 12,
        "end_line": 13,
        "segment_type": "operational_concern",
        "content": "## Consequences\nTeam must train on PostgreSQL.",
        "confidence": 0.75,
    },
]

_GOOD_RESPONSE = json.dumps(_GOOD_SEGMENTS)

_LOW_CONFIDENCE_SEGMENTS = [
    {
        "start_line": 1,
        "end_line": 3,
        "segment_type": "decision",
        "content": "Some text",
        "confidence": 0.2,  # below threshold — should become unknown
    },
]

_LOW_CONFIDENCE_RESPONSE = json.dumps(_LOW_CONFIDENCE_SEGMENTS)


class _MockBridge(ModelInferenceAdapter):
    """Controllable mock inference bridge supporting sequential responses."""

    def __init__(self, responses: list[str | Exception]) -> None:
        self._responses = list(responses)
        self._call_count = 0
        self.calls: list[dict[str, object]] = []

    async def infer(
        self,
        model_key: str,
        system_prompt: str,
        user_prompt: str,
        timeout_seconds: float,
        temperature: float | None = None,
    ) -> str:
        if self._call_count >= len(self._responses):
            raise RuntimeError("Unexpected extra infer() call")
        self.calls.append(
            {
                "model_key": model_key,
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "timeout_seconds": timeout_seconds,
                "temperature": temperature,
            }
        )
        response = self._responses[self._call_count]
        self._call_count += 1
        if isinstance(response, Exception):
            raise response
        return response

    @property
    def call_count(self) -> int:
        return self._call_count


def _make_request(**overrides: object) -> ModelSegmentationRequest:
    defaults: dict[str, object] = {
        "source_path": _SOURCE_PATH,
        "source_content": _SOURCE_CONTENT,
        "source_content_sha256": _SOURCE_SHA,
        "correlation_id": "corr-001",
    }
    defaults.update(overrides)
    return ModelSegmentationRequest(**defaults)


# ---------------------------------------------------------------------------
# Helper unit tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_sha256_is_deterministic() -> None:
    assert _sha256("hello") == _sha256("hello")
    assert _sha256("hello") != _sha256("world")


@pytest.mark.unit
def test_compute_segment_id_is_deterministic() -> None:
    sid = _compute_segment_id("path/a.md", "abc123", 1, 5, "decision")
    assert sid == _compute_segment_id("path/a.md", "abc123", 1, 5, "decision")
    assert sid != _compute_segment_id("path/a.md", "abc123", 1, 5, "background")


@pytest.mark.unit
def test_build_user_prompt_includes_line_numbers() -> None:
    request = _make_request()
    prompt = _build_user_prompt(request)
    assert "1: # ADR-001" in prompt
    assert _SOURCE_PATH in prompt


# ---------------------------------------------------------------------------
# _parse_segments unit tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_parse_segments_happy_path() -> None:
    request = _make_request()
    segments = _parse_segments(_GOOD_RESPONSE, request, 0.4)
    assert segments is not None
    assert len(segments) == 5
    assert segments[3].segment_type == EnumSegmentType.decision
    assert segments[3].confidence == pytest.approx(0.95)
    assert len(segments[3].segment_id) == 64  # sha256 hex


@pytest.mark.unit
def test_parse_segments_segment_id_deterministic() -> None:
    request = _make_request()
    s1 = _parse_segments(_GOOD_RESPONSE, request, 0.4)
    s2 = _parse_segments(_GOOD_RESPONSE, request, 0.4)
    assert s1 is not None
    assert s2 is not None
    assert s1[0].segment_id == s2[0].segment_id


@pytest.mark.unit
def test_parse_segments_segment_content_sha256_correct() -> None:
    request = _make_request()
    segments = _parse_segments(_GOOD_RESPONSE, request, 0.4)
    assert segments is not None
    for seg in segments:
        assert seg.segment_content_sha256 == _sha256(seg.content)


@pytest.mark.unit
def test_parse_segments_low_confidence_becomes_unknown() -> None:
    request = _make_request()
    segments = _parse_segments(_LOW_CONFIDENCE_RESPONSE, request, 0.4)
    assert segments is not None
    assert len(segments) == 1
    assert segments[0].segment_type == EnumSegmentType.unknown
    assert segments[0].confidence == pytest.approx(0.2)


@pytest.mark.unit
def test_parse_segments_unknown_type_falls_back_to_unknown() -> None:
    raw = json.dumps(
        [
            {
                "start_line": 1,
                "end_line": 2,
                "segment_type": "totally_made_up_type",
                "content": "text",
                "confidence": 0.9,
            }
        ]
    )
    request = _make_request()
    segments = _parse_segments(raw, request, 0.4)
    assert segments is not None
    assert segments[0].segment_type == EnumSegmentType.unknown


@pytest.mark.unit
def test_parse_segments_invalid_json_returns_none() -> None:
    assert _parse_segments("not json", _make_request(), 0.4) is None


@pytest.mark.unit
def test_parse_segments_not_array_returns_none() -> None:
    assert _parse_segments('{"key": "val"}', _make_request(), 0.4) is None


@pytest.mark.unit
def test_parse_segments_missing_required_field_returns_none() -> None:
    raw = json.dumps(
        [{"start_line": 1, "end_line": 2, "segment_type": "decision", "content": "x"}]
    )
    # missing "confidence"
    assert _parse_segments(raw, _make_request(), 0.4) is None


@pytest.mark.unit
def test_parse_segments_invalid_line_range_returns_none() -> None:
    raw = json.dumps(
        [
            {
                "start_line": 5,
                "end_line": 2,  # end < start
                "segment_type": "decision",
                "content": "x",
                "confidence": 0.9,
            }
        ]
    )
    assert _parse_segments(raw, _make_request(), 0.4) is None


@pytest.mark.unit
def test_parse_segments_out_of_bounds_line_range_returns_none() -> None:
    raw = json.dumps(
        [
            {
                "start_line": 1,
                "end_line": 999,
                "segment_type": "decision",
                "content": "x",
                "confidence": 0.9,
            }
        ]
    )
    assert _parse_segments(raw, _make_request(), 0.4) is None


@pytest.mark.unit
def test_parse_segments_strips_markdown_fences() -> None:
    fenced = f"```json\n{_GOOD_RESPONSE}\n```"
    request = _make_request()
    segments = _parse_segments(fenced, request, 0.4)
    assert segments is not None
    assert len(segments) == 5


# ---------------------------------------------------------------------------
# HandlerSegmentation happy path
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_success_returns_segments() -> None:
    bridge = _MockBridge([_GOOD_RESPONSE])
    handler = HandlerSegmentation(inference_bridge=bridge)
    request = _make_request()

    result = await handler.handle(request)

    assert isinstance(result, ModelSegmentationResult)
    assert result.success is True
    assert result.correlation_id == "corr-001"
    assert result.source_path == _SOURCE_PATH
    assert len(result.segments) == 5
    assert result.error_code is None
    assert result.error_message is None
    assert bridge.call_count == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_success_records_evidence() -> None:
    bridge = _MockBridge([_GOOD_RESPONSE])
    handler = HandlerSegmentation(
        inference_bridge=bridge,
        prompt_template_id="adr_segmentation_v1",
        prompt_template_version="1.0.0",
        segmentation_model_key="qwen3-coder",
    )
    result = await handler.handle(_make_request())

    assert result.llm_call_evidence is not None
    ev = result.llm_call_evidence
    assert isinstance(ev, ModelLLMCallEvidence)
    assert ev.prompt_template_id == "adr_segmentation_v1"
    assert ev.prompt_template_version == "1.0.0"
    assert ev.model_key == "qwen3-coder"
    assert len(ev.prompt_hash) == 64
    assert len(ev.input_hash) == 64
    assert len(ev.response_hash) == 64
    assert ev.json_repair_attempted is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_evidence_hashes_are_correct() -> None:
    bridge = _MockBridge([_GOOD_RESPONSE])
    handler = HandlerSegmentation(inference_bridge=bridge)
    request = _make_request()

    result = await handler.handle(request)

    assert result.llm_call_evidence is not None
    assert result.llm_call_evidence.input_hash == _sha256(request.source_content)
    assert result.llm_call_evidence.response_hash == _sha256(_GOOD_RESPONSE)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_uses_configured_prompt_threshold_and_temperature() -> None:
    bridge = _MockBridge([_GOOD_RESPONSE])
    handler = HandlerSegmentation(
        inference_bridge=bridge,
        low_confidence_threshold=0.55,
        segmentation_temperature=0.15,
    )

    result = await handler.handle(_make_request())

    system_prompt = bridge.calls[0]["system_prompt"]
    assert isinstance(system_prompt, str)
    assert result.success is True
    assert "confidence < 0.55" in system_prompt
    assert bridge.calls[0]["temperature"] == pytest.approx(0.15)


# ---------------------------------------------------------------------------
# Low-confidence → UNKNOWN policy
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_low_confidence_segment_becomes_unknown() -> None:
    bridge = _MockBridge([_LOW_CONFIDENCE_RESPONSE])
    handler = HandlerSegmentation(inference_bridge=bridge, low_confidence_threshold=0.4)
    result = await handler.handle(_make_request())

    assert result.success is True
    assert len(result.segments) == 1
    assert result.segments[0].segment_type == EnumSegmentType.unknown


# ---------------------------------------------------------------------------
# JSON repair policy
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_malformed_json_triggers_repair_attempt() -> None:
    bridge = _MockBridge(["this is not json", _GOOD_RESPONSE])
    handler = HandlerSegmentation(inference_bridge=bridge)
    result = await handler.handle(_make_request())

    assert result.success is True
    assert len(result.segments) == 5
    assert bridge.call_count == 2
    assert result.llm_call_evidence is not None
    assert result.llm_call_evidence.json_repair_attempted is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_both_attempts_fail_returns_parse_failed() -> None:
    bridge = _MockBridge(["not json", "also not json"])
    handler = HandlerSegmentation(inference_bridge=bridge)
    result = await handler.handle(_make_request())

    assert result.success is False
    assert result.error_code == "SEGMENTATION_PARSE_FAILED"
    assert "repair attempt" in (result.error_message or "")
    assert bridge.call_count == 2
    assert result.llm_call_evidence is not None
    assert result.llm_call_evidence.json_repair_attempted is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_repair_llm_failure_returns_parse_failed() -> None:
    bridge = _MockBridge(["not json", RuntimeError("network error")])
    handler = HandlerSegmentation(inference_bridge=bridge)
    result = await handler.handle(_make_request())

    assert result.success is False
    assert result.error_code == "SEGMENTATION_PARSE_FAILED"


# ---------------------------------------------------------------------------
# LLM failure paths — must NOT return empty success
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_llm_failure_returns_success_false_not_empty_segments() -> None:
    bridge = _MockBridge([RuntimeError("connection refused")])
    handler = HandlerSegmentation(inference_bridge=bridge)
    result = await handler.handle(_make_request())

    assert result.success is False
    assert result.error_code == "SEGMENTATION_LLM_CALL_FAILED"
    assert result.error_message is not None
    assert "connection refused" in result.error_message
    assert result.segments == []
    assert result.llm_call_evidence is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_llm_failure_includes_model_id() -> None:
    bridge = _MockBridge([RuntimeError("timeout")])
    handler = HandlerSegmentation(
        inference_bridge=bridge,
        segmentation_model_key="qwen3-coder",
    )
    result = await handler.handle(_make_request())

    assert result.success is False
    assert result.model_id == "qwen3-coder"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_timeout_error_is_retryable() -> None:
    bridge = _MockBridge([TimeoutError("timed out")])
    handler = HandlerSegmentation(inference_bridge=bridge)
    result = await handler.handle(_make_request())

    assert result.success is False
    assert result.retryable is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_runtime_error_is_not_retryable() -> None:
    bridge = _MockBridge([RuntimeError("bad request")])
    handler = HandlerSegmentation(inference_bridge=bridge)
    result = await handler.handle(_make_request())

    assert result.success is False
    assert result.retryable is False


# ---------------------------------------------------------------------------
# Model validation
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_segmentation_request_is_frozen() -> None:
    from pydantic import ValidationError

    request = _make_request()
    with pytest.raises((ValidationError, TypeError)):
        request.correlation_id = "mutated"  # type: ignore[misc]


@pytest.mark.unit
def test_segmentation_result_success_false_has_empty_segments() -> None:
    result = ModelSegmentationResult(
        correlation_id="c1",
        source_path="path/a.md",
        success=False,
        error_code="SEGMENTATION_LLM_CALL_FAILED",
        error_message="failed",
        model_id="qwen3-coder",
    )
    assert result.segments == []


@pytest.mark.unit
def test_segmentation_request_rejects_invalid_source_sha() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _make_request(source_content_sha256="not-a-sha")


@pytest.mark.unit
def test_segmentation_result_rejects_failure_without_error_fields() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ModelSegmentationResult(
            correlation_id="c1",
            source_path="path/a.md",
            success=False,
            model_id="qwen3-coder",
        )


@pytest.mark.unit
def test_document_segment_confidence_bounds() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ModelDocumentSegment(
            segment_id="abc",
            source_path="p",
            source_content_sha256="sha",
            start_line=1,
            end_line=2,
            segment_type=EnumSegmentType.decision,
            content="text",
            segment_content_sha256="sha2",
            confidence=1.5,  # out of range
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_from_contract_reads_config_defaults() -> None:
    """from_contract wires config correctly — verified via evidence on success."""
    contract = {
        "config": {
            "segmentation_model_key": {"default": "custom-model"},
            "segmentation_timeout_seconds": {"default": 60.0},
            "low_confidence_threshold": {"default": 0.5},
            "prompt_template_id": {"default": "my_template_v2"},
            "prompt_template_version": {"default": "2.0.0"},
        }
    }
    bridge = _MockBridge([_GOOD_RESPONSE])
    handler = HandlerSegmentation.from_contract(contract)
    handler._bridge = bridge  # noqa: SLF001 — test-only mock injection

    result = await handler.handle(_make_request())

    assert result.success is True
    assert result.llm_call_evidence is not None
    ev = result.llm_call_evidence
    assert ev.model_key == "custom-model"
    assert ev.prompt_template_id == "my_template_v2"
    assert ev.prompt_template_version == "2.0.0"


# ---------------------------------------------------------------------------
# OMN-10871: DI fallback — AdapterInferenceBridge construction annotated
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_handler_segmentation_init_type_annotation_is_typed() -> None:
    """inference_bridge param is typed as ModelInferenceAdapter | None, not Any."""
    import inspect

    sig = inspect.signature(HandlerSegmentation.__init__)
    param = sig.parameters["inference_bridge"]
    assert param.annotation is not inspect.Parameter.empty
    annotation_str = str(param.annotation)
    assert "ModelInferenceAdapter" in annotation_str
    assert "Any" not in annotation_str
