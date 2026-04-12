"""Golden chain test for node_ci_watch.

Verifies the handler produces correct terminal states in dry_run mode
and can be wired to EventBusInmemory for golden chain validation.
All tests use dry_run=True — no subprocess calls to gh CLI.
"""

from __future__ import annotations

import json

import pytest

from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnimarket.nodes.node_ci_watch.handlers.handler_ci_watch import (
    EnumCiTerminalStatus,
    HandlerCiWatch,
    ModelCiWatchCommand,
)

CMD_TOPIC = "onex.cmd.omnimarket.ci-watch-start.v1"
EVT_TOPIC = "onex.evt.omnimarket.ci-watch-completed.v1"


@pytest.mark.unit
class TestCiWatchGoldenChain:
    """Golden chain: command -> handler -> completion event."""

    async def test_dry_run_returns_passed(self, event_bus: EventBusInmemory) -> None:
        """dry_run mode should always return terminal_status=passed."""
        handler = HandlerCiWatch()
        command = ModelCiWatchCommand(
            pr_number=42,
            repo="OmniNode-ai/omniclaude",
            correlation_id="test-corr-001",
            dry_run=True,
        )
        result = handler.handle(command)

        assert result.terminal_status == EnumCiTerminalStatus.PASSED
        assert result.failed_checks == []
        assert result.failure_summary == ""
        assert result.dry_run is True
        assert result.pr_number == 42
        assert result.repo == "OmniNode-ai/omniclaude"

    async def test_dry_run_preserves_correlation_id(
        self, event_bus: EventBusInmemory
    ) -> None:
        """correlation_id should round-trip through the result."""
        handler = HandlerCiWatch()
        command = ModelCiWatchCommand(
            pr_number=99,
            repo="OmniNode-ai/omnimarket",
            correlation_id="unique-corr-xyz",
            dry_run=True,
        )
        result = handler.handle(command)

        assert result.correlation_id == "unique-corr-xyz"

    async def test_dry_run_timestamps_set(self, event_bus: EventBusInmemory) -> None:
        """started_at and completed_at should both be set."""
        handler = HandlerCiWatch()
        command = ModelCiWatchCommand(
            pr_number=1,
            repo="OmniNode-ai/omniclaude",
            correlation_id="ts-test",
            dry_run=True,
        )
        result = handler.handle(command)

        assert result.started_at is not None
        assert result.completed_at is not None
        assert result.completed_at >= result.started_at

    async def test_event_bus_wiring(self, event_bus: EventBusInmemory) -> None:
        """Handler can be wired to event bus and process command events."""
        handler = HandlerCiWatch()
        results_captured: list[dict] = []  # type: ignore[type-arg]

        async def on_command(message: object) -> None:
            payload = json.loads(message.value)  # type: ignore[union-attr]
            command = ModelCiWatchCommand(**payload)
            result = handler.handle(command)
            result_payload = result.model_dump(mode="json")
            results_captured.append(result_payload)
            await event_bus.publish(
                EVT_TOPIC,
                key=None,
                value=json.dumps(result_payload).encode(),
            )

        await event_bus.start()
        await event_bus.subscribe(
            CMD_TOPIC, on_message=on_command, group_id="test-ci-watch"
        )

        cmd_payload = json.dumps(
            {
                "pr_number": 42,
                "repo": "OmniNode-ai/omniclaude",
                "correlation_id": "bus-test-001",
                "dry_run": True,
            }
        ).encode()
        await event_bus.publish(CMD_TOPIC, key=None, value=cmd_payload)

        assert len(results_captured) == 1
        assert results_captured[0]["terminal_status"] == "passed"

        history = await event_bus.get_event_history(topic=EVT_TOPIC)
        assert len(history) == 1

        await event_bus.close()

    async def test_result_serializes_to_json(self, event_bus: EventBusInmemory) -> None:
        """Result should serialize cleanly to JSON."""
        handler = HandlerCiWatch()
        command = ModelCiWatchCommand(
            pr_number=5,
            repo="OmniNode-ai/omnibase_core",
            correlation_id="json-test",
            dry_run=True,
        )
        result = handler.handle(command)
        serialized = result.model_dump_json()

        parsed = json.loads(serialized)
        assert parsed["terminal_status"] == "passed"
        assert parsed["dry_run"] is True

    async def test_timeout_minutes_propagated(
        self, event_bus: EventBusInmemory
    ) -> None:
        """timeout_minutes should be accepted without error."""
        handler = HandlerCiWatch()
        command = ModelCiWatchCommand(
            pr_number=10,
            repo="OmniNode-ai/omniclaude",
            correlation_id="timeout-test",
            timeout_minutes=120,
            dry_run=True,
        )
        result = handler.handle(command)

        assert result.terminal_status == EnumCiTerminalStatus.PASSED
