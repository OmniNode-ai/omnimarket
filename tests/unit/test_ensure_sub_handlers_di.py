# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""TDD tests for OMN-8450: _ensure_sub_handlers() DI injection skeleton."""

from __future__ import annotations

import pytest


@pytest.mark.unit
class TestEnsureSubHandlersDI:
    def test_ensure_sub_handlers_resolves_all_5_slots(self) -> None:
        """After _ensure_sub_handlers(), all 5 protocol slots must be non-None."""
        from omnimarket.nodes.node_overnight.handlers.handler_overnight import (
            HandlerBuildLoopExecutor,
        )
        from omnimarket.nodes.node_overnight.protocols.protocol_phase_handlers import (
            ProtocolBuildLoopPhaseHandler,
            ProtocolCiWatchHandler,
            ProtocolMergeSweepHandler,
            ProtocolNightlyLoopHandler,
            ProtocolPlatformReadinessHandler,
        )

        handler = HandlerBuildLoopExecutor()
        handler._ensure_sub_handlers()

        assert handler._nightly_loop is not None
        assert handler._build_loop is not None
        assert handler._merge_sweep is not None
        assert handler._ci_watch is not None
        assert handler._platform_readiness is not None

        # Each resolved slot must implement the correct protocol
        assert isinstance(handler._nightly_loop, ProtocolNightlyLoopHandler)
        assert isinstance(handler._build_loop, ProtocolBuildLoopPhaseHandler)
        assert isinstance(handler._merge_sweep, ProtocolMergeSweepHandler)
        assert isinstance(handler._ci_watch, ProtocolCiWatchHandler)
        assert isinstance(handler._platform_readiness, ProtocolPlatformReadinessHandler)

    def test_ensure_sub_handlers_raises_on_missing_handler(self) -> None:
        """Explicitly passing a broken slot must raise DependencyResolutionError."""
        from omnimarket.nodes.node_overnight.handlers.handler_overnight import (
            HandlerBuildLoopExecutor,
        )
        from omnimarket.nodes.node_overnight.protocols.di import (
            DependencyResolutionError,
        )

        # Simulate missing handler by patching _resolve_nightly_loop to raise
        handler = HandlerBuildLoopExecutor()

        def _raise() -> None:
            raise DependencyResolutionError("nightly_loop", "stub not available")

        handler._resolve_nightly_loop = _raise  # type: ignore[method-assign]

        with pytest.raises(DependencyResolutionError):
            handler._ensure_sub_handlers()

    def test_ensure_sub_handlers_no_event_bus_none(self) -> None:
        """_ensure_sub_handlers() must not leave event_bus=None on handler."""
        from unittest.mock import MagicMock

        from omnimarket.nodes.node_overnight.handlers.handler_overnight import (
            HandlerBuildLoopExecutor,
        )

        mock_bus = MagicMock()
        handler = HandlerBuildLoopExecutor(event_bus=mock_bus)
        handler._ensure_sub_handlers()

        # event_bus must remain the injected value, never overwritten with None
        assert handler._event_bus is mock_bus
        assert handler._event_bus is not None
