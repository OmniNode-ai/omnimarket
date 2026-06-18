# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Golden-chain test for node_pr_semantic_grader_llm_effect.

Exercises the full effect chain end to end without a live broker or LLM:

    cmd envelope (subscribe topic)
        -> ModelSemanticGradingRequest deserialization
        -> HandlerPrSemanticGrader.handle(...)  (injected fake inference bridge)
        -> ModelSemanticGradingResult
        -> evt payload (publish topic) round-trips through JSON

The inference bridge is injected per the handler's documented test-injection
path so no network call is made. The chain asserts the contract's declared
subscribe/publish topics stay aligned with the handler's behavior, and that a
grader failure yields a typed failure evt rather than zero scores.

Added by OMN-13208 (A1): the inference bridge re-home repointed this node's
handler import to ``omnimarket.inference``; this golden chain proves the
re-homed bridge still drives the live effect path.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from omnimarket.inference.adapter_inference_bridge import ModelInferenceAdapter
from omnimarket.nodes.node_pr_semantic_grader_llm_effect.handlers.handler_pr_semantic_grader import (
    HandlerPrSemanticGrader,
)
from omnimarket.nodes.node_pr_semantic_grader_llm_effect.models.model_semantic_grading_request import (
    ModelSemanticGradingRequest,
)
from omnimarket.nodes.node_pr_semantic_grader_llm_effect.models.model_semantic_grading_result import (
    ModelSemanticGradingResult,
)

_CONTRACT_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_pr_semantic_grader_llm_effect"
    / "contract.yaml"
)

_EXPECTED_SUBSCRIBE_TOPIC = "onex.cmd.omnimarket.pr-semantic-grading-requested.v1"
_EXPECTED_PUBLISH_TOPIC = "onex.evt.omnimarket.pr-semantic-grading-completed.v1"

_CRITERIA = [
    "Topic names must be resolved via contract-driven lookup, not hardcoded strings.",
    "Handler must implement ProtocolMessageHandler.handle(envelope).",
]

_CLEAN_DIFF = """\
--- a/src/handler.py
+++ b/src/handler.py
@@ -10,6 +10,8 @@ class HandlerFoo:
     async def handle(self, request):
+        topic = self._contract.get_subscribe_topic("foo_requested")
         await self._bus.publish(topic, request)
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


class _FakeInferenceBridge(ModelInferenceAdapter):
    """In-process bridge returning a canned valid grading response."""

    def __init__(self, response: str | Exception) -> None:
        self._response = response
        self.calls: list[dict[str, object]] = []

    async def infer(
        self,
        model_key: str,
        system_prompt: str,
        user_prompt: str,
        timeout_seconds: float,
        temperature: float | None = None,
    ) -> str:
        self.calls.append(
            {
                "model_key": model_key,
                "user_prompt": user_prompt,
                "timeout_seconds": timeout_seconds,
            }
        )
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def _load_contract() -> dict[str, object]:
    return yaml.safe_load(_CONTRACT_PATH.read_text())


def _cmd_envelope() -> dict[str, object]:
    """The command payload a producer would publish to the subscribe topic."""
    return {
        "ticket_id": "OMN-13208",
        "acceptance_criteria": _CRITERIA,
        "pr_diff_text": _CLEAN_DIFF,
        "pr_title": "feat(OMN-13208): A1 inference-bridge re-home",
        "correlation_id": "golden-chain-pr-semantic-grader",
    }


@pytest.mark.unit
def test_contract_topics_match_chain_expectations() -> None:
    """Contract subscribe/publish topics are the ones the chain rides on."""
    contract = _load_contract()
    event_bus = contract["event_bus"]
    assert isinstance(event_bus, dict)
    assert event_bus["subscribe_topics"] == [_EXPECTED_SUBSCRIBE_TOPIC]
    assert event_bus["publish_topics"] == [_EXPECTED_PUBLISH_TOPIC]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_golden_chain_cmd_to_evt_produces_graded_result() -> None:
    """cmd envelope -> handler -> evt payload, no broker, no network."""
    bridge = _FakeInferenceBridge(_GOOD_RESPONSE)
    handler = HandlerPrSemanticGrader(inference_bridge=bridge)

    request = ModelSemanticGradingRequest.model_validate(_cmd_envelope())
    result = await handler.handle(request)

    assert isinstance(result, ModelSemanticGradingResult)
    assert result.success is True
    assert result.correlation_id == "golden-chain-pr-semantic-grader"
    assert result.ticket_id == "OMN-13208"
    assert result.criteria_coverage == 0.9
    assert result.advisory is False
    assert result.llm_call_evidence is not None

    # The re-homed bridge was driven through the real handler path exactly once.
    assert len(bridge.calls) == 1

    # The evt payload published to the completed topic round-trips through JSON.
    evt_payload = json.loads(result.model_dump_json())
    assert evt_payload["success"] is True
    assert evt_payload["correlation_id"] == "golden-chain-pr-semantic-grader"
    assert evt_payload["criteria_coverage"] == 0.9


@pytest.mark.unit
@pytest.mark.asyncio
async def test_golden_chain_llm_failure_yields_typed_failure_evt() -> None:
    """A bridge failure produces a typed failure evt, never zero scores."""
    bridge = _FakeInferenceBridge(ConnectionError("broker unreachable"))
    handler = HandlerPrSemanticGrader(inference_bridge=bridge)

    request = ModelSemanticGradingRequest.model_validate(_cmd_envelope())
    result = await handler.handle(request)

    assert result.success is False
    assert result.error_code == "GRADER_LLM_CALL_FAILED"
    assert result.criteria_coverage is None
    evt_payload = json.loads(result.model_dump_json())
    assert evt_payload["success"] is False
