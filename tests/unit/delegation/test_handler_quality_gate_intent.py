# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for HandlerQualityGateIntent (OMN-12294).

Verifies the contract-native quality gate handler that replaces the in-process
DelegationIntentBridge.handle_quality_gate_intent() path.
"""

from __future__ import annotations

import json
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

_SCOUT_REPORT = json.dumps(
    {
        "role": "scout",
        "verdict": "found",
        "findings_paths": ["tests/unit/delegation/test_gap.py"],
        "summary": (
            "Investigated the reported coverage gap and confirmed a missing "
            "null-check regression test at the cited path."
        ),
    }
)


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

    def test_marker_only_content_returns_reject_only_result(self) -> None:
        handler = HandlerQualityGateIntent()
        intent = self._intent(_GOOD_RESEARCH)

        result = handler.handle(intent)

        assert isinstance(result, ModelQualityGateResult)
        assert result.correlation_id == intent.payload.correlation_id
        assert result.passed is False
        assert result.quality_score == pytest.approx(1.0)
        assert any("reject-only" in r for r in result.failure_reasons)

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

    def test_agent_delegation_bus_intent_rejects_non_report_shape(self) -> None:
        """OMN-15196 per-hop proof (OMN-15180 lesson): the BUS-INTENT hop --
        not just the local dispatch port -- resolves the ``agent_delegation``
        task-class declared default response contract (per-role dispatch
        report, OMN-15161) with NO wire-model change, keyed on task_type
        alone. Plain prose is not even valid JSON, let alone one of the four
        report shapes, so it fails schema validation (MALFORMED, not the old
        REFUSAL/sub_tasks_verified keyword categories)."""
        handler = HandlerQualityGateIntent()
        intent = ModelQualityGateIntent(
            payload=ModelQualityGateInput(
                correlation_id=uuid4(),
                task_type="agent_delegation",
                llm_response_content="Task completed, everything looks fine.",
            )
        )

        result = handler.handle(intent)

        assert result.passed is False
        assert any("MALFORMED" in r for r in result.failure_reasons)
        assert not any("REFUSAL" in r for r in result.failure_reasons)

    def test_agent_delegation_bus_intent_rejects_wrong_shaped_json(self) -> None:
        """A well-formed JSON object that is NOT one of the four report shapes
        fails SCHEMA_VIOLATION specifically -- the exact live-reproduced
        OMN-15193 false-positive case (a "rationale" containing "i cannot")
        would have PASSED the retired no_refusal-only path incorrectly for a
        report-shaped caller; the declared contract instead correctly rejects
        it on STRUCTURE (missing role/verdict/summary/etc.), not on a
        refusal-adjacent substring."""
        handler = HandlerQualityGateIntent()
        tactical_json = (
            '{"action": "hold_position", "action_params": {"unit_id": "u-17"}, '
            '"confidence": 0.82, "rationale": "i cannot confirm the enemy flank '
            'is clear, so holding position is the lower-risk tactical choice."}'
        )
        intent = ModelQualityGateIntent(
            payload=ModelQualityGateInput(
                correlation_id=uuid4(),
                task_type="agent_delegation",
                llm_response_content=tactical_json,
            )
        )

        result = handler.handle(intent)

        assert result.passed is False
        assert any("SCHEMA_VIOLATION" in r for r in result.failure_reasons)
        assert not any("REFUSAL" in r for r in result.failure_reasons)

    def test_agent_delegation_bus_intent_accepts_real_dispatch_report(self) -> None:
        """GREEN counterpart: a response shaped as a real dispatch-worker
        report passes the bus-intent hop with no caller-declared contract."""
        handler = HandlerQualityGateIntent()
        intent = ModelQualityGateIntent(
            payload=ModelQualityGateInput(
                correlation_id=uuid4(),
                task_type="agent_delegation",
                llm_response_content=_SCOUT_REPORT,
            )
        )

        result = handler.handle(intent)

        assert result.passed is True
        assert result.failure_reasons == ()

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
