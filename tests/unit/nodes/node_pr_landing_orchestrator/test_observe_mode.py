# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Observe mode (OMN-22921): the node tick runs beside the script and writes no merge, arm or rerun."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from omnimarket.models.landing_decision import ModelLandingDecision
from omnimarket.nodes.node_pr_landing_tick_orchestrator.handlers.handler_pr_landing_tick_orchestrator import (
    HandlerPrLandingTickOrchestrator,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.handlers.tick_ports import (
    InMemoryTickWriter,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.models.model_tick_orchestrator import (
    EnumTickFailureReason,
    EnumTickMode,
    EnumTickStatus,
    ModelTickRequest,
    ModelTickResult,
    ModelTickSnapshot,
)

pytestmark = pytest.mark.unit

REAL_TICK = (
    Path(__file__).resolve().parents[3]
    / "fixtures"
    / "pr_landing_tick_replay"
    / "tick_725_omnimarket.json"
)


def _snapshot() -> ModelTickSnapshot:
    return ModelTickSnapshot.model_validate_json(REAL_TICK.read_text(encoding="utf-8"))


def _request(mode: EnumTickMode) -> ModelTickRequest:
    snap = _snapshot()
    return ModelTickRequest(
        correlation_id=uuid4(),
        mode=mode,
        facts=snap.facts,
        recorded_decision=snap.decision,
        fair_share=snap.fair_share,
    )


def assert_wrote_nothing(writer: InMemoryTickWriter, result: ModelTickResult) -> None:
    """The observe reading: no merge, arm or rerun reached the writer, and the result says so."""
    assert writer.performed == [], f"writer was called: {writer.performed}"
    assert result.writes_performed == 0


class LeakyOrchestrator(HandlerPrLandingTickOrchestrator):
    """Named injected break: an observe tick that performs the actions it computed."""

    def _performed_in(
        self, request: ModelTickRequest, decision: ModelLandingDecision
    ) -> int:
        count = 0
        for action in decision.actions:
            self._writer.perform(action)
            count += 1
        return count


def test_observe_tick_computes_the_decision_and_writes_nothing() -> None:
    writer = InMemoryTickWriter()
    result = HandlerPrLandingTickOrchestrator(writer=writer).handle(
        _request(EnumTickMode.OBSERVE)
    )
    assert result.status is EnumTickStatus.COMPARED
    assert result.mode is EnumTickMode.OBSERVE
    assert result.node_actions > 0
    assert_wrote_nothing(writer, result)


def test_replay_tick_writes_nothing_either() -> None:
    writer = InMemoryTickWriter()
    result = HandlerPrLandingTickOrchestrator(writer=writer).handle(
        _request(EnumTickMode.REPLAY)
    )
    assert_wrote_nothing(writer, result)


def test_act_mode_is_refused_while_the_overlay_does_not_enable_it() -> None:
    writer = InMemoryTickWriter()
    result = HandlerPrLandingTickOrchestrator(writer=writer).handle(
        _request(EnumTickMode.ACT)
    )
    assert result.status is EnumTickStatus.FAILED
    assert result.failure_reason is EnumTickFailureReason.ACT_NOT_ENABLED
    assert_wrote_nothing(writer, result)


def test_control_the_observe_reading_goes_red_on_the_named_injected_break() -> None:
    writer = InMemoryTickWriter()
    result = LeakyOrchestrator(writer=writer).handle(_request(EnumTickMode.OBSERVE))
    with pytest.raises(AssertionError):
        assert_wrote_nothing(writer, result)
