# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Tests for HandlerPrSemanticGrader."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_hostile_reviewer.handlers.adapter_inference_bridge import (
    ModelInferenceAdapter,
)
from omnimarket.nodes.node_pr_semantic_grader_llm_effect.handlers.handler_pr_semantic_grader import (
    HandlerPrSemanticGrader,
    _parse_scores,
)
from omnimarket.nodes.node_pr_semantic_grader_llm_effect.models.model_semantic_grading_request import (
    ModelSemanticGradingRequest,
)
from omnimarket.nodes.node_pr_semantic_grader_llm_effect.models.model_semantic_grading_result import (
    ModelLLMCallEvidence,
    ModelSemanticGradingResult,
)

# ---------------------------------------------------------------------------
# Fixtures derived from OMN-10834 — hardcoded strings in PR (anti-pattern present)
# ---------------------------------------------------------------------------

# OMN-10834 required contract-driven resolution for topic/event_type lookups.
# The offending diff hardcoded both — this fixture exercises the anti-pattern path.
_OMN_10834_CRITERIA = [
    "Topic names must be resolved via contract-driven lookup, not hardcoded strings.",
    "Event type values must be read from the contract YAML, not inlined as literals.",
    "Handler must use ContractConfigExtractor or equivalent to read topic/event_type.",
    "No bare string literals for Kafka topic names in handler code.",
]

_OFFENDING_DIFF = """\
--- a/src/handler.py
+++ b/src/handler.py
@@ -10,6 +10,10 @@ class HandlerFoo:
+    TOPIC = "onex.cmd.omnimarket.foo-requested.v1"
+    EVENT_TYPE = "foo_requested"
+
     async def handle(self, request):
-        topic = self._contract.get_topic("foo_requested")
+        topic = "onex.cmd.omnimarket.foo-requested.v1"
+        event_type = "foo_requested"
         await self._bus.publish(topic, {"event_type": event_type})
"""

_CLEAN_DIFF = """\
--- a/src/handler.py
+++ b/src/handler.py
@@ -10,6 +10,10 @@ class HandlerFoo:
     async def handle(self, request):
+        extractor = ContractConfigExtractor(self._contract)
+        topic = extractor.get_subscribe_topic("foo_requested")
+        event_type = extractor.get_event_type("foo_requested")
         await self._bus.publish(topic, {"event_type": event_type})
"""

_GOOD_RESPONSE = json.dumps(
    {
        "criteria_coverage": 0.9,
        "contract_alignment": 0.85,
        "anti_pattern_present": 0.1,
        "overall_confidence": 0.9,
        "rationale": "Good coverage|Contract-driven|No violations|High confidence",
    }
)

_ANTI_PATTERN_RESPONSE = json.dumps(
    {
        "criteria_coverage": 0.2,
        "contract_alignment": 0.1,
        "anti_pattern_present": 0.9,
        "overall_confidence": 0.95,
        "rationale": "Criteria not met|Hardcoded strings|Severe violations|High confidence",
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


def _make_request(**overrides: object) -> ModelSemanticGradingRequest:
    defaults: dict[str, object] = {
        "ticket_id": "OMN-10834",
        "acceptance_criteria": _OMN_10834_CRITERIA,
        "pr_diff_text": _CLEAN_DIFF,
        "pr_title": "feat(OMN-10834): contract-driven topic resolution",
        "correlation_id": "corr-001",
    }
    defaults.update(overrides)
    return ModelSemanticGradingRequest(**defaults)


# ---------------------------------------------------------------------------
# _parse_scores unit tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_parse_scores_valid() -> None:
    raw = json.dumps(
        {
            "criteria_coverage": 0.9,
            "contract_alignment": 0.8,
            "anti_pattern_present": 0.1,
            "overall_confidence": 0.85,
        }
    )
    result = _parse_scores(raw)
    assert result is not None
    scores, rationale = result
    assert scores["criteria_coverage"] == pytest.approx(0.9)
    assert scores["contract_alignment"] == pytest.approx(0.8)
    assert scores["anti_pattern_present"] == pytest.approx(0.1)
    assert scores["overall_confidence"] == pytest.approx(0.85)
    assert rationale is None


@pytest.mark.unit
def test_parse_scores_with_rationale() -> None:
    raw = json.dumps(
        {
            "criteria_coverage": 0.9,
            "contract_alignment": 0.8,
            "anti_pattern_present": 0.1,
            "overall_confidence": 0.85,
            "rationale": "Good|Contract-driven|Clean|Confident",
        }
    )
    result = _parse_scores(raw)
    assert result is not None
    _, rationale = result
    assert rationale == "Good|Contract-driven|Clean|Confident"


@pytest.mark.unit
def test_parse_scores_missing_key_returns_none() -> None:
    raw = json.dumps(
        {
            "criteria_coverage": 0.9,
            "contract_alignment": 0.8,
            "anti_pattern_present": 0.1,
        }
    )
    assert _parse_scores(raw) is None


@pytest.mark.unit
def test_parse_scores_out_of_range_returns_none() -> None:
    raw = json.dumps(
        {
            "criteria_coverage": 1.5,
            "contract_alignment": 0.8,
            "anti_pattern_present": 0.1,
            "overall_confidence": 0.85,
        }
    )
    assert _parse_scores(raw) is None


@pytest.mark.unit
def test_parse_scores_invalid_json_returns_none() -> None:
    assert _parse_scores("not json at all") is None


@pytest.mark.unit
def test_parse_scores_boolean_values_rejected() -> None:
    raw = json.dumps(
        {
            "criteria_coverage": True,
            "contract_alignment": 0.8,
            "anti_pattern_present": 0.1,
            "overall_confidence": 0.85,
        }
    )
    assert _parse_scores(raw) is None


# ---------------------------------------------------------------------------
# HandlerPrSemanticGrader happy path
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_clean_diff_returns_low_anti_pattern() -> None:
    """Clean diff (contract-driven lookup) must score anti_pattern_present < 0.3."""
    bridge = _MockBridge(_GOOD_RESPONSE)
    handler = HandlerPrSemanticGrader(inference_bridge=bridge)
    result = await handler.handle(_make_request(pr_diff_text=_CLEAN_DIFF))

    assert result.success is True
    assert result.anti_pattern_present is not None
    assert result.anti_pattern_present < 0.3
    assert result.advisory is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_offending_diff_raises_advisory_flag() -> None:
    """OMN-10834 offending diff (hardcoded strings) must score anti_pattern_present >= 0.7."""
    bridge = _MockBridge(_ANTI_PATTERN_RESPONSE)
    handler = HandlerPrSemanticGrader(inference_bridge=bridge)
    result = await handler.handle(
        _make_request(
            pr_diff_text=_OFFENDING_DIFF,
            acceptance_criteria=_OMN_10834_CRITERIA,
        )
    )

    assert result.success is True
    assert result.anti_pattern_present is not None
    assert result.anti_pattern_present >= 0.7
    assert result.advisory is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_success_records_evidence() -> None:
    bridge = _MockBridge(_GOOD_RESPONSE)
    handler = HandlerPrSemanticGrader(
        inference_bridge=bridge,
        prompt_template_id="pr_semantic_grading_v1",
        prompt_template_version="1.0.0",
        grader_model_key="opus",
    )
    result = await handler.handle(_make_request())

    assert result.llm_call_evidence is not None
    assert isinstance(result.llm_call_evidence, ModelLLMCallEvidence)
    assert result.llm_call_evidence.prompt_template_id == "pr_semantic_grading_v1"
    assert result.llm_call_evidence.grader_model_key == "opus"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_success_preserves_correlation_and_ticket() -> None:
    bridge = _MockBridge(_GOOD_RESPONSE)
    handler = HandlerPrSemanticGrader(inference_bridge=bridge)
    result = await handler.handle(
        _make_request(correlation_id="corr-xyz", ticket_id="OMN-9999")
    )

    assert result.correlation_id == "corr-xyz"
    assert result.ticket_id == "OMN-9999"


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_llm_failure_returns_success_false_not_zero_scores() -> None:
    bridge = _MockBridge(RuntimeError("connection refused"))
    handler = HandlerPrSemanticGrader(inference_bridge=bridge)
    result = await handler.handle(_make_request())

    assert result.success is False
    assert result.error_code == "GRADER_LLM_CALL_FAILED"
    assert result.error_message is not None
    assert "connection refused" in result.error_message
    assert result.criteria_coverage is None
    assert result.anti_pattern_present is None
    assert result.advisory is False
    assert result.llm_call_evidence is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_parse_failure_returns_success_false() -> None:
    bridge = _MockBridge("I cannot evaluate this diff.")
    handler = HandlerPrSemanticGrader(inference_bridge=bridge)
    result = await handler.handle(_make_request())

    assert result.success is False
    assert result.error_code == "GRADER_PARSE_FAILED"
    assert result.criteria_coverage is None
    assert result.llm_call_evidence is None


# ---------------------------------------------------------------------------
# Model validation
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_request_is_frozen() -> None:
    request = _make_request()
    with pytest.raises((ValidationError, TypeError)):
        request.correlation_id = "mutated"  # type: ignore[misc]


@pytest.mark.unit
def test_result_advisory_threshold_at_exactly_07() -> None:
    result = ModelSemanticGradingResult.with_scores(
        correlation_id="c1",
        ticket_id="OMN-1",
        criteria_coverage=0.5,
        contract_alignment=0.5,
        anti_pattern_present=0.7,
        overall_confidence=0.8,
        rationale=None,
        llm_call_evidence=None,
    )
    assert result.advisory is True


@pytest.mark.unit
def test_result_advisory_false_below_threshold() -> None:
    result = ModelSemanticGradingResult.with_scores(
        correlation_id="c1",
        ticket_id="OMN-1",
        criteria_coverage=0.9,
        contract_alignment=0.9,
        anti_pattern_present=0.69,
        overall_confidence=0.9,
        rationale=None,
        llm_call_evidence=None,
    )
    assert result.advisory is False


@pytest.mark.unit
def test_result_scores_must_be_in_range() -> None:
    with pytest.raises(ValidationError):
        ModelSemanticGradingResult(
            correlation_id="c1",
            ticket_id="OMN-1",
            success=True,
            criteria_coverage=1.5,  # out of range
            contract_alignment=0.8,
            anti_pattern_present=0.1,
            overall_confidence=0.9,
        )
