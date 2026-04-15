# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain test for node_session_compose."""

from __future__ import annotations

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from pydantic import ValidationError

from omnimarket.nodes.node_session_compose.handlers.handler_session_compose import (
    HandlerSessionCompose,
)
from omnimarket.nodes.node_session_compose.models.model_session_compose_command import (
    ModelSessionComposeCommand,
)


class TestGoldenChainSessionCompose:
    """Golden chain coverage for the session compose scaffold."""

    async def test_dry_run_session_compose(self) -> None:
        bus = EventBusInmemory()
        handler = HandlerSessionCompose(event_bus=bus)
        cmd = ModelSessionComposeCommand(
            phases=["platform_readiness", "pipeline_fill"], dry_run=True
        )

        result = await handler.handle(cmd)

        assert result.success is True
        assert result.dry_run is True
        assert len(result.phase_results) == 2
        assert [r.phase for r in result.phase_results] == [
            "platform_readiness",
            "pipeline_fill",
        ]
        assert all(r.status == "dry_run" for r in result.phase_results)

    async def test_live_dispatch_returns_placeholder(self) -> None:
        handler = HandlerSessionCompose()
        cmd = ModelSessionComposeCommand(
            phases=["platform_readiness", "pipeline_fill", "ticket_pipeline"],
            dry_run=False,
        )

        result = await handler.handle(cmd)

        assert result.success is True
        assert result.dry_run is False
        assert len(result.phase_results) == 3
        assert all(r.status == "dispatched" for r in result.phase_results)

    def test_command_rejects_empty_phases(self) -> None:
        with pytest.raises(ValidationError, match="phases"):
            ModelSessionComposeCommand(phases=[], dry_run=True)

    def test_command_rejects_blank_phase(self) -> None:
        with pytest.raises(ValidationError, match="phase"):
            ModelSessionComposeCommand(
                phases=["platform_readiness", "  "], dry_run=True
            )

    async def test_handler_without_event_bus(self) -> None:
        handler = HandlerSessionCompose()
        cmd = ModelSessionComposeCommand(phases=["platform_readiness"], dry_run=True)
        result = await handler.handle(cmd)
        assert result.success is True
        assert len(result.phase_results) == 1

    async def test_publishes_to_success_topic_when_bus_present(self) -> None:
        captured: list[tuple[str, object]] = []

        class _StubBus:
            async def publish_envelope(
                self, *, envelope: object, topic: str, **_: object
            ) -> None:
                captured.append((topic, envelope))

        handler = HandlerSessionCompose(event_bus=_StubBus())
        cmd = ModelSessionComposeCommand(phases=["platform_readiness"], dry_run=True)

        result = await handler.handle(cmd)

        assert result.success is True
        assert len(captured) == 1
        topic, envelope = captured[0]
        assert topic == "onex.evt.omnimarket.session-compose-completed.v1"
        assert envelope is result
