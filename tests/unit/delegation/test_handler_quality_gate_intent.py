# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for HandlerQualityGateIntent (OMN-12294).

Verifies the contract-native quality gate handler that replaces the in-process
DelegationIntentBridge.handle_quality_gate_intent() path.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from omnibase_core.models.delegation.wire import (
    ModelQualityGateInput,
    ModelQualityGateIntent,
)

from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate_intent import (
    TOPIC_QUALITY_GATE_RESULT,
    HandlerQualityGateIntent,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_result import (
    ModelQualityGateResult,
)

_GOOD_RESEARCH = (
    "Line 42 shows the runtime ingress boundary where validation should happen "
    "before dispatch. The tradeoff is that strict validation rejects more "
    "requests up front, but the benefit is clearer evidence and lower risk of "
    "malformed payloads entering the event bus."
)
_REFUSAL = "I cannot help with that request. As an AI, I'm sorry."


@pytest.mark.unit
class TestHandlerQualityGateIntent:
    def _intent(self, content: str) -> ModelQualityGateIntent:
        # The contract declares event_model=ModelQualityGateIntent, so the runtime
        # validates the payload and hands handle() the typed intent.
        return ModelQualityGateIntent(
            payload=ModelQualityGateInput(
                correlation_id=uuid4(),
                task_type="research",
                llm_response_content=content,
            )
        )

    def test_passing_content_returns_passed_result(self) -> None:
        handler = HandlerQualityGateIntent()
        intent = self._intent(_GOOD_RESEARCH)

        result = handler.handle(intent)

        assert isinstance(result, ModelQualityGateResult)
        assert result.correlation_id == intent.payload.correlation_id
        assert result.passed is True

    def test_refusal_content_fails_gate(self) -> None:
        handler = HandlerQualityGateIntent()
        result = handler.handle(self._intent(_REFUSAL))

        assert result.passed is False
        assert any("REFUSAL" in r for r in result.failure_reasons)

    def test_returns_result_for_runtime_autopublish(self) -> None:
        # The runtime dispatch-result applier publishes the RETURNED model to the
        # contract's published_events topic (quality-gate-result.v1).
        handler = HandlerQualityGateIntent()
        result = handler.handle(self._intent(_GOOD_RESEARCH))
        assert isinstance(result, ModelQualityGateResult)
        assert TOPIC_QUALITY_GATE_RESULT.endswith("quality-gate-result.v1")

    def test_contract_declares_quality_gate_result_publish_topic(self) -> None:
        from omnimarket.nodes.contract_topics import contract_publish_topics

        contract = (
            Path(__file__).parents[3]
            / "src"
            / "omnimarket"
            / "nodes"
            / "node_delegation_quality_gate_reducer"
            / "contract.yaml"
        )
        topics = contract_publish_topics(contract)
        assert any(t.endswith("quality-gate-result.v1") for t in topics)
