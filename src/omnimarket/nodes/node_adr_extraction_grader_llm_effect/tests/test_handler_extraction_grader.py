# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Tests for HandlerExtractionGrader."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_adr_extraction_grader_llm_effect.handlers.handler_extraction_grader import (
    HandlerExtractionGrader,
    _parse_scores,
)
from omnimarket.nodes.node_adr_extraction_grader_llm_effect.models.model_grading_request import (
    ModelGradingRequest,
)
from omnimarket.nodes.node_adr_extraction_grader_llm_effect.models.model_grading_result import (
    ModelGradingResult,
    ModelLLMCallEvidence,
)
from omnimarket.nodes.node_hostile_reviewer.handlers.adapter_inference_bridge import (
    ModelInferenceAdapter,
)

_GROUND_TRUTH = (
    "# ADR-001: Use PostgreSQL for primary storage\n\n"
    "## Status\nAccepted\n\n"
    "## Context\nWe need a reliable RDBMS for transactional data.\n\n"
    "## Decision\nUse PostgreSQL 15 as the primary database.\n\n"
    "## Consequences\nTeam must be trained on PostgreSQL administration."
)

_SOURCE_DOC = (
    "We evaluated several databases and decided on PostgreSQL for reliability."
)

_EXTRACTION_OUTPUT = [
    {
        "decision": "Use PostgreSQL 15 as primary database",
        "status": "Accepted",
        "rationale": "Reliability and team familiarity",
    }
]

_GOOD_RESPONSE = json.dumps(
    {
        "recall": 0.9,
        "precision": 0.85,
        "fidelity": 0.95,
        "format_compliance": 1.0,
        "rationale": "Good recall|High precision|Faithful|Schema correct",
    }
)


class _MockBridge(ModelInferenceAdapter):
    """Controllable mock inference bridge."""

    def __init__(self, response: str | Exception) -> None:
        self._response = response

    async def infer(
        self,
        model_key: str,
        system_prompt: str,
        user_prompt: str,
        timeout_seconds: float,
        temperature: float | None = None,
    ) -> str:
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def _make_request(**overrides: object) -> ModelGradingRequest:
    defaults: dict[str, object] = {
        "ground_truth_adr": _GROUND_TRUTH,
        "extraction_output": _EXTRACTION_OUTPUT,
        "source_document": _SOURCE_DOC,
        "correlation_id": "corr-001",
        "model_key_under_test": "qwen3-coder",
    }
    defaults.update(overrides)
    return ModelGradingRequest(**defaults)


# ---------------------------------------------------------------------------
# _parse_scores unit tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_parse_scores_valid() -> None:
    raw = json.dumps(
        {"recall": 0.9, "precision": 0.8, "fidelity": 0.7, "format_compliance": 1.0}
    )
    scores = _parse_scores(raw)
    assert scores is not None
    assert scores["recall"] == pytest.approx(0.9)
    assert scores["precision"] == pytest.approx(0.8)
    assert scores["fidelity"] == pytest.approx(0.7)
    assert scores["format_compliance"] == pytest.approx(1.0)


@pytest.mark.unit
def test_parse_scores_embedded_in_prose() -> None:
    raw = (
        'Here is my evaluation: {"recall": 0.75, "precision": 0.6, '
        '"fidelity": 0.8, "format_compliance": 0.9} Hope that helps.'
    )
    scores = _parse_scores(raw)
    assert scores is not None
    assert scores["recall"] == pytest.approx(0.75)


@pytest.mark.unit
def test_parse_scores_missing_key_returns_none() -> None:
    raw = json.dumps({"recall": 0.9, "precision": 0.8, "fidelity": 0.7})
    assert _parse_scores(raw) is None


@pytest.mark.unit
def test_parse_scores_out_of_range_returns_none() -> None:
    raw = json.dumps(
        {"recall": 1.5, "precision": 0.8, "fidelity": 0.7, "format_compliance": 0.9}
    )
    assert _parse_scores(raw) is None


@pytest.mark.unit
def test_parse_scores_invalid_json_returns_none() -> None:
    assert _parse_scores("not json at all") is None


@pytest.mark.unit
def test_parse_scores_empty_string_returns_none() -> None:
    assert _parse_scores("") is None


@pytest.mark.unit
def test_parse_scores_boolean_values_rejected() -> None:
    raw = json.dumps(
        {"recall": True, "precision": 0.8, "fidelity": 0.7, "format_compliance": 0.9}
    )
    assert _parse_scores(raw) is None


# ---------------------------------------------------------------------------
# HandlerExtractionGrader happy path
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_success_returns_scores() -> None:
    bridge = _MockBridge(_GOOD_RESPONSE)
    handler = HandlerExtractionGrader(inference_bridge=bridge)
    request = _make_request()

    result = await handler.handle(request)

    assert isinstance(result, ModelGradingResult)
    assert result.success is True
    assert result.correlation_id == "corr-001"
    assert result.model_key_under_test == "qwen3-coder"
    assert result.recall == pytest.approx(0.9)
    assert result.precision == pytest.approx(0.85)
    assert result.fidelity == pytest.approx(0.95)
    assert result.format_compliance == pytest.approx(1.0)
    assert result.error_code is None
    assert result.error_message is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_success_records_evidence() -> None:
    bridge = _MockBridge(_GOOD_RESPONSE)
    handler = HandlerExtractionGrader(
        inference_bridge=bridge,
        prompt_template_id="adr_grading_v1",
        prompt_template_version="1.0.0",
        grader_model_key="opus",
    )
    result = await handler.handle(_make_request())

    assert result.llm_call_evidence is not None
    assert isinstance(result.llm_call_evidence, ModelLLMCallEvidence)
    assert result.llm_call_evidence.prompt_template_id == "adr_grading_v1"
    assert result.llm_call_evidence.prompt_template_version == "1.0.0"
    assert result.llm_call_evidence.grader_model_key == "opus"


# ---------------------------------------------------------------------------
# Failure path: grader LLM call fails -- must NOT return zero scores
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_llm_failure_returns_success_false_not_zero_scores() -> None:
    bridge = _MockBridge(RuntimeError("connection refused"))
    handler = HandlerExtractionGrader(inference_bridge=bridge)
    result = await handler.handle(_make_request())

    assert result.success is False
    assert result.error_code == "GRADER_LLM_CALL_FAILED"
    assert result.error_message is not None
    assert "connection refused" in result.error_message
    # Scores must be None -- zero score is indistinguishable from poor extraction
    assert result.recall is None
    assert result.precision is None
    assert result.fidelity is None
    assert result.format_compliance is None
    assert result.llm_call_evidence is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_parse_failure_returns_success_false() -> None:
    bridge = _MockBridge("I cannot score this extraction.")
    handler = HandlerExtractionGrader(inference_bridge=bridge)
    result = await handler.handle(_make_request())

    assert result.success is False
    assert result.error_code == "GRADER_PARSE_FAILED"
    assert result.recall is None
    assert result.llm_call_evidence is None


# ---------------------------------------------------------------------------
# Model validation
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_grading_request_is_frozen() -> None:
    request = _make_request()
    with pytest.raises((ValidationError, TypeError)):
        request.correlation_id = "mutated"  # type: ignore[misc]


@pytest.mark.unit
def test_grading_result_success_false_has_no_scores() -> None:
    result = ModelGradingResult(
        correlation_id="c1",
        model_key_under_test="m1",
        success=False,
        error_code="GRADER_LLM_CALL_FAILED",
        error_message="failed",
    )
    assert result.recall is None
    assert result.precision is None


@pytest.mark.unit
def test_grading_result_scores_must_be_in_range() -> None:
    with pytest.raises(ValidationError):
        ModelGradingResult(
            correlation_id="c1",
            model_key_under_test="m1",
            success=True,
            recall=1.5,  # out of range
            precision=0.8,
            fidelity=0.9,
            format_compliance=1.0,
        )
