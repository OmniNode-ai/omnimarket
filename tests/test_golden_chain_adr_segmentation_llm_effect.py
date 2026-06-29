# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Golden-chain test for node_adr_segmentation_llm_effect.

Exercises the full effect chain end to end over a RECORDED-FROM-REAL inference
replay (OMN-13498 B1), never a hand-written canned fake:

    cmd envelope (subscribe topic)
        -> ModelSegmentationRequest deserialization
        -> HandlerSegmentation.handle(...)  (RecordedReplayInferenceAdapter)
        -> ModelSegmentationResult
        -> evt payload (publish topic) round-trips through JSON

The inference response replayed here was CAPTURED FROM A REAL z.ai GLM
(``cloud-glm``, ``glm-5.2``) call resolved through the committed routing contract
(see ``tests/fixtures/inference_replay/glm_adr_segmentation.json`` +
``tests/fixtures/inference_replay/__init__.py``). The replay adapter HARD-REJECTS
a delegation tier name handed in as a ``model_key``, so it cannot mask the
tier-name-as-model_key regression a hand-written fake would (OMN-13470 /
OMN-13497 ``check-no-faked-boundary``). The chain asserts the contract's declared
subscribe/publish topics stay aligned with what the handler consumes/produces,
and that the real recorded segmentation yields a JSON-serializable evt payload.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml

from omnimarket.inference.adapter_inference_bridge import (
    ModelInferenceAdapter,
)
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
from tests.fixtures.inference_replay import (
    RecordedReplayInferenceAdapter,
    load_recorded_response,
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

# The real recorded GLM segmentation response (replayed, never canned). Captured
# from a live cloud-glm call resolved through the routing contract.
_RECORDED_FIXTURE = "glm_adr_segmentation.json"
_LLM_RESPONSE = load_recorded_response(_RECORDED_FIXTURE)


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
    """cmd envelope -> handler -> evt payload over a real recorded replay."""
    bridge = RecordedReplayInferenceAdapter(_RECORDED_FIXTURE)
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
