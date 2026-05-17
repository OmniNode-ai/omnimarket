# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for HandlerBuildLoopOrchestrator DI injection seams (OMN-10750).

Verifies:
- fsm: HandlerBuildLoop is injectable and used when injected
- overseer_verifier: HandlerOverseerVerifier is injectable and used when injected
- Both default to concrete instances when not injected (no crash)
- No split-identity: _overseer_verifier is the single wired reference
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from omnimarket.nodes.node_build_loop.handlers.handler_build_loop import (
    HandlerBuildLoop,
)
from omnimarket.nodes.node_build_loop_orchestrator.handlers.handler_build_loop_orchestrator import (
    HandlerBuildLoopOrchestrator,
)
from omnimarket.nodes.node_overseer_verifier.handlers.handler_overseer_verifier import (
    HandlerOverseerVerifier,
)


def _make_event_bus() -> MagicMock:
    bus = MagicMock()
    bus.publish = MagicMock(return_value=None)
    bus.publish_envelope = MagicMock(return_value=None)
    return bus


@pytest.mark.unit
class TestHandlerBuildLoopOrchestratorDIInjection:
    """Verify FSM and overseer verifier injection seams."""

    def test_injected_fsm_is_used(self) -> None:
        """When fsm is injected, the orchestrator must hold that exact instance."""
        fsm = HandlerBuildLoop()
        orch = HandlerBuildLoopOrchestrator(
            event_bus=_make_event_bus(),
            fsm=fsm,
        )
        assert orch._fsm is fsm

    def test_default_fsm_constructed_when_not_injected(self) -> None:
        """When no fsm is passed, a default HandlerBuildLoop is constructed."""
        orch = HandlerBuildLoopOrchestrator(event_bus=_make_event_bus())
        assert isinstance(orch._fsm, HandlerBuildLoop)

    def test_injected_overseer_verifier_is_used(self) -> None:
        """When overseer_verifier is injected, the orchestrator must hold that exact instance."""
        verifier = HandlerOverseerVerifier()
        orch = HandlerBuildLoopOrchestrator(
            event_bus=_make_event_bus(),
            overseer_verifier=verifier,
        )
        assert orch._overseer_verifier is verifier

    def test_default_overseer_verifier_constructed_when_not_injected(self) -> None:
        """When no overseer_verifier is passed, a default HandlerOverseerVerifier is constructed."""
        orch = HandlerBuildLoopOrchestrator(event_bus=_make_event_bus())
        assert isinstance(orch._overseer_verifier, HandlerOverseerVerifier)

    def test_no_split_identity_advisory_overseer_removed(self) -> None:
        """_advisory_overseer attribute no longer exists — single wired reference only."""
        orch = HandlerBuildLoopOrchestrator(event_bus=_make_event_bus())
        assert not hasattr(orch, "_advisory_overseer")

    def test_both_injected_simultaneously(self) -> None:
        """Both fsm and overseer_verifier can be injected together."""
        fsm = HandlerBuildLoop()
        verifier = HandlerOverseerVerifier()
        orch = HandlerBuildLoopOrchestrator(
            event_bus=_make_event_bus(),
            fsm=fsm,
            overseer_verifier=verifier,
        )
        assert orch._fsm is fsm
        assert orch._overseer_verifier is verifier

    def test_mock_overseer_verifier_is_called(self) -> None:
        """_run_advisory_overseer_check calls self._overseer_verifier.verify."""
        import uuid

        mock_verifier = MagicMock(spec=HandlerOverseerVerifier)
        mock_verifier.verify.return_value = {"verdict": "PASS", "summary": "ok"}

        orch = HandlerBuildLoopOrchestrator(
            event_bus=_make_event_bus(),
            overseer_verifier=mock_verifier,
        )
        orch._run_advisory_overseer_check(
            correlation_id=uuid.uuid4(),
            classified_count=3,
        )

        mock_verifier.verify.assert_called_once()
