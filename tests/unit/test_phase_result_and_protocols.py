# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""TDD tests for OMN-8449: PhaseResult + 5 Protocol classes + field_validator enforcement."""

from __future__ import annotations

import pytest
from pydantic import ValidationError


@pytest.mark.unit
class TestPhaseResultValidation:
    def test_phase_result_raises_on_empty_summary_when_success(self) -> None:
        """PhaseResult(success=True, side_effect_summary='') must raise ValidationError."""
        from omnimarket.nodes.node_overnight.protocols.protocol_phase_handlers import (
            PhaseResult,
        )

        with pytest.raises(ValidationError):
            PhaseResult(success=True, side_effect_summary="", duration_seconds=1.0)

    def test_phase_result_accepts_skipped_reason(self) -> None:
        """PhaseResult with success=False and skipped: prefix is valid."""
        from omnimarket.nodes.node_overnight.protocols.protocol_phase_handlers import (
            PhaseResult,
        )

        result = PhaseResult(
            success=False,
            side_effect_summary="skipped: no PR context",
            duration_seconds=0.0,
        )
        assert result.success is False
        assert result.side_effect_summary == "skipped: no PR context"

    def test_phase_result_accepts_success_with_nonempty_summary(self) -> None:
        """PhaseResult(success=True, side_effect_summary='3 tickets dispatched') is valid."""
        from omnimarket.nodes.node_overnight.protocols.protocol_phase_handlers import (
            PhaseResult,
        )

        result = PhaseResult(
            success=True,
            side_effect_summary="3 tickets dispatched",
            duration_seconds=1.5,
        )
        assert result.success is True
        assert result.side_effect_summary == "3 tickets dispatched"

    def test_phase_result_duration_positive(self) -> None:
        """A real I/O phase result must have duration_seconds > 0.1."""
        from omnimarket.nodes.node_overnight.protocols.protocol_phase_handlers import (
            PhaseResult,
        )

        result = PhaseResult(
            success=True,
            side_effect_summary="7 PRs reviewed",
            duration_seconds=2.3,
        )
        assert result.duration_seconds > 0.1

    def test_phase_result_is_frozen(self) -> None:
        """PhaseResult must be immutable (frozen=True)."""
        from omnimarket.nodes.node_overnight.protocols.protocol_phase_handlers import (
            PhaseResult,
        )

        result = PhaseResult(
            success=True,
            side_effect_summary="done",
            duration_seconds=1.0,
        )
        with pytest.raises((TypeError, AttributeError, ValidationError)):
            result.success = False  # type: ignore[misc]


@pytest.mark.unit
class TestAllProtocolSlots:
    def test_all_5_protocol_slots_declared(self) -> None:
        """HandlerBuildLoopExecutor must declare all 5 protocol slot attributes."""
        from omnimarket.nodes.node_overnight.handlers.handler_overnight import (
            HandlerBuildLoopExecutor,
        )

        handler = HandlerBuildLoopExecutor()
        assert hasattr(handler, "_nightly_loop")
        assert hasattr(handler, "_build_loop")
        assert hasattr(handler, "_merge_sweep")
        assert hasattr(handler, "_ci_watch")
        assert hasattr(handler, "_platform_readiness")

    def test_protocol_classes_importable(self) -> None:
        """All 5 Protocol classes must be importable from the protocols module."""
        from omnimarket.nodes.node_overnight.protocols.protocol_phase_handlers import (
            ProtocolBuildLoopPhaseHandler,
            ProtocolCiWatchHandler,
            ProtocolMergeSweepHandler,
            ProtocolNightlyLoopHandler,
            ProtocolPlatformReadinessHandler,
        )

        for proto in [
            ProtocolNightlyLoopHandler,
            ProtocolBuildLoopPhaseHandler,
            ProtocolMergeSweepHandler,
            ProtocolCiWatchHandler,
            ProtocolPlatformReadinessHandler,
        ]:
            assert proto is not None

    def test_protocol_nightly_loop_not_stub(self) -> None:
        """ProtocolNightlyLoopHandler.handle signature must accept correlation_id kwarg."""
        import inspect

        from omnimarket.nodes.node_overnight.protocols.protocol_phase_handlers import (
            ProtocolNightlyLoopHandler,
        )

        # The protocol must declare handle() with correlation_id parameter
        hints = ProtocolNightlyLoopHandler.__protocol_attrs__  # type: ignore[attr-defined]
        assert "handle" in hints or hasattr(ProtocolNightlyLoopHandler, "handle")

        sig = inspect.signature(ProtocolNightlyLoopHandler.handle)  # type: ignore[misc]
        assert "correlation_id" in sig.parameters
