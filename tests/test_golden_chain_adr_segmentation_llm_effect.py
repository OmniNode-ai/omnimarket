# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Golden-chain test for node_adr_segmentation_llm_effect.

Exercises the full effect chain end to end without a live broker or LLM:

    cmd envelope (subscribe topic)
        -> ModelSegmentationRequest deserialization
        -> HandlerSegmentation.handle(...)  (injected fake inference bridge)
        -> ModelSegmentationResult
        -> evt payload (publish topic) round-trips through JSON

The inference bridge is injected per the handler's documented test-injection
path so no network call is made. The chain asserts the contract's declared
subscribe/publish topics stay aligned with what the handler consumes/produces,
and that a successful segmentation yields a JSON-serializable evt payload.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_adr_segmentation_llm_effect.handlers.handler_segmentation import (
    HandlerSegmentation,
)
from omnimarket.nodes.node_adr_segmentation_llm_effect.models.model_segmentation_request import (
    ModelSegmentationRequest,
)
from omnimarket.nodes.node_adr_segmentation_llm_effect.models.model_segmentation_result import (
    EnumSegmentType,
    ModelSegmentationResult,
)
from omnimarket.nodes.node_hostile_reviewer.handlers.adapter_inference_bridge import (
    ModelInferenceAdapter,
)

_CONTRACT_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_adr_segmentation_llm_effect"
    / "contract.yaml"
)

_EXPECTED_SUBSCRIBE_TOPIC = "onex.cmd.omnimarket.adr-segmentation-requested.v1"
_EXPECTED_PUBLISH_TOPIC = "onex.evt.omnimarket.adr-segmentation-completed.v1"

_SOURCE_CONTENT = (
    "# ADR-007: Adopt the bus as the inter-service transport\n"
    "\n"
    "## Decision\n"
    "All inter-service traffic flows over Kafka topics.\n"
)
_SOURCE_PATH = "docs/adr/adr-007-bus-transport.md"
_SOURCE_SHA = hashlib.sha256(_SOURCE_CONTENT.encode()).hexdigest()

_LLM_RESPONSE = json.dumps(
    [
        {
            "start_line": 1,
            "end_line": 1,
            "segment_type": "background",
            "content": "# ADR-007: Adopt the bus as the inter-service transport",
            "confidence": 0.9,
        },
        {
            "start_line": 3,
            "end_line": 4,
            "segment_type": "decision",
            "content": "## Decision\nAll inter-service traffic flows over Kafka topics.",
            "confidence": 0.95,
        },
    ]
)


class _FakeInferenceBridge(ModelInferenceAdapter):
    """In-process bridge returning a canned valid segmentation response."""

    def __init__(self, response: str) -> None:
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
                "temperature": temperature,
            }
        )
        return self._response


def _load_contract() -> dict[str, object]:
    return yaml.safe_load(_CONTRACT_PATH.read_text())


def _cmd_envelope() -> dict[str, object]:
    """The command payload a producer would publish to the subscribe topic."""
    return {
        "source_path": _SOURCE_PATH,
        "source_content": _SOURCE_CONTENT,
        "source_content_sha256": _SOURCE_SHA,
        "correlation_id": "golden-chain-adr-seg",
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
async def test_golden_chain_cmd_to_evt_produces_segmented_result() -> None:
    """cmd envelope -> handler -> evt payload, no broker, no network."""
    bridge = _FakeInferenceBridge(_LLM_RESPONSE)
    handler = HandlerSegmentation(inference_bridge=bridge)

    # Subscribe-topic envelope deserializes into the request contract.
    request = ModelSegmentationRequest.model_validate(_cmd_envelope())

    result = await handler.handle(request)

    # Handler produced a typed, successful result.
    assert isinstance(result, ModelSegmentationResult)
    assert result.success is True
    assert result.correlation_id == "golden-chain-adr-seg"
    assert result.source_path == _SOURCE_PATH
    assert len(result.segments) == 2
    assert result.segments[1].segment_type == EnumSegmentType.decision
    assert result.llm_call_evidence is not None

    # The bridge was driven through the real handler path exactly once.
    assert len(bridge.calls) == 1

    # The evt payload published to the completed topic round-trips through JSON.
    evt_payload = json.loads(result.model_dump_json())
    assert evt_payload["success"] is True
    assert evt_payload["correlation_id"] == "golden-chain-adr-seg"
    assert len(evt_payload["segments"]) == 2
    # Every segment id is a deterministic sha256 hex digest.
    for segment in evt_payload["segments"]:
        assert len(segment["segment_id"]) == 64


@pytest.mark.unit
@pytest.mark.asyncio
async def test_golden_chain_llm_failure_yields_typed_failure_evt() -> None:
    """A bridge failure produces a typed failure evt, never an empty success."""

    class _FailingBridge(ModelInferenceAdapter):
        async def infer(
            self,
            model_key: str,
            system_prompt: str,
            user_prompt: str,
            timeout_seconds: float,
            temperature: float | None = None,
        ) -> str:
            raise ConnectionError("broker unreachable")

    handler = HandlerSegmentation(inference_bridge=_FailingBridge())
    request = ModelSegmentationRequest.model_validate(_cmd_envelope())

    result = await handler.handle(request)

    assert result.success is False
    assert result.error_code == "SEGMENTATION_LLM_CALL_FAILED"
    assert result.retryable is True
    assert result.segments == []

    evt_payload = json.loads(result.model_dump_json())
    assert evt_payload["success"] is False
    assert evt_payload["error_code"] == "SEGMENTATION_LLM_CALL_FAILED"
