# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for HandlerOvernight self-perpetuating loop trigger (OMN-8407).

Covers the enable_self_loop / loop_delay_seconds contract:
- When enable_self_loop=True and an event_bus is wired, the handler re-emits
  onex.cmd.omnimarket.overnight-start.v1 after each run.
- The requeued command carries delay_seconds and enable_self_loop=True so the
  loop continues indefinitely.
- When enable_self_loop=False or no event_bus is wired, no requeue is emitted.
- The requeue fires on all terminal states (completed, partial, failed) so the
  loop keeps turning even when phases fail.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from omnimarket.nodes.node_overnight.handlers.handler_overnight import (
    EnumPhase,
    HandlerOvernight,
    ModelOvernightCommand,
)

TOPIC_OVERNIGHT_COMPLETE = "onex.evt.omnimarket.overnight-session-completed.v1"
TOPIC_OVERNIGHT_START = "onex.cmd.omnimarket.overnight-start.v1"


class _RecordingBus:
    """Sync event-bus stub that captures (topic, payload_dict) tuples."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, topic: str, payload: bytes) -> None:
        self.calls.append((topic, json.loads(payload.decode())))

    def by_topic(self, topic: str) -> list[dict[str, Any]]:
        return [p for t, p in self.calls if t == topic]


def _cmd(**overrides: Any) -> ModelOvernightCommand:
    defaults: dict[str, Any] = {
        "correlation_id": "test-self-loop",
        "dry_run": True,
    }
    defaults.update(overrides)
    return ModelOvernightCommand(**defaults)


@pytest.mark.unit
class TestSelfLoopTrigger:
    """Self-loop requeue behavior."""

    def test_self_loop_emits_start_command_by_default(self) -> None:
        bus = _RecordingBus()
        handler = HandlerOvernight(event_bus=bus)

        handler.handle(_cmd())

        start_calls = bus.by_topic(TOPIC_OVERNIGHT_START)
        assert len(start_calls) == 1, (
            f"Expected 1 start requeue, got {len(start_calls)}"
        )

    def test_self_loop_requeue_carries_delay_seconds(self) -> None:
        bus = _RecordingBus()
        handler = HandlerOvernight(event_bus=bus)

        handler.handle(_cmd(loop_delay_seconds=600))

        start_calls = bus.by_topic(TOPIC_OVERNIGHT_START)
        assert len(start_calls) == 1
        assert start_calls[0]["delay_seconds"] == 600

    def test_self_loop_requeue_preserves_loop_delay_seconds(self) -> None:
        bus = _RecordingBus()
        handler = HandlerOvernight(event_bus=bus)

        handler.handle(_cmd(loop_delay_seconds=180))

        start_calls = bus.by_topic(TOPIC_OVERNIGHT_START)
        assert start_calls[0]["loop_delay_seconds"] == 180

    def test_self_loop_requeue_sets_enable_self_loop_true(self) -> None:
        """Requeued command must continue the loop, not stop it."""
        bus = _RecordingBus()
        handler = HandlerOvernight(event_bus=bus)

        handler.handle(_cmd())

        start_calls = bus.by_topic(TOPIC_OVERNIGHT_START)
        assert start_calls[0]["enable_self_loop"] is True

    def test_self_loop_requeue_gets_fresh_correlation_id(self) -> None:
        """Each requeue must get a new correlation_id to avoid dedup collisions."""
        bus = _RecordingBus()
        handler = HandlerOvernight(event_bus=bus)

        handler.handle(_cmd(correlation_id="original-id"))

        start_calls = bus.by_topic(TOPIC_OVERNIGHT_START)
        assert start_calls[0]["correlation_id"] != "original-id"

    def test_self_loop_disabled_emits_no_start_command(self) -> None:
        bus = _RecordingBus()
        handler = HandlerOvernight(event_bus=bus)

        handler.handle(_cmd(enable_self_loop=False))

        start_calls = bus.by_topic(TOPIC_OVERNIGHT_START)
        assert len(start_calls) == 0, "enable_self_loop=False must not requeue"

    def test_no_event_bus_no_self_loop_no_error(self) -> None:
        handler = HandlerOvernight()  # event_bus=None

        result = handler.handle(_cmd())
        assert result.session_status.value == "completed"

    def test_self_loop_fires_after_complete_envelope(self) -> None:
        """Requeue must come AFTER the session-complete envelope."""
        bus = _RecordingBus()
        handler = HandlerOvernight(event_bus=bus)

        handler.handle(_cmd())

        topics = [t for t, _ in bus.calls]
        complete_idx = topics.index(TOPIC_OVERNIGHT_COMPLETE)
        start_idx = topics.index(TOPIC_OVERNIGHT_START)
        assert start_idx > complete_idx, (
            "Requeue must follow the session-complete envelope"
        )

    def test_self_loop_fires_on_partial_status(self) -> None:
        """Loop must continue even when session ends as PARTIAL."""
        bus = _RecordingBus()
        handler = HandlerOvernight(event_bus=bus)

        handler.handle(
            _cmd(dry_run=False),
            phase_results={
                EnumPhase.NIGHTLY_LOOP: True,
                EnumPhase.BUILD_LOOP: True,
                EnumPhase.MERGE_SWEEP: False,
                EnumPhase.CI_WATCH: True,
                EnumPhase.PLATFORM_READINESS: True,
            },
        )

        start_calls = bus.by_topic(TOPIC_OVERNIGHT_START)
        assert len(start_calls) == 1, "Self-loop must fire even on PARTIAL status"

    def test_self_loop_fires_on_failed_status(self) -> None:
        """Loop must continue even when session ends as FAILED."""
        bus = _RecordingBus()
        handler = HandlerOvernight(event_bus=bus)

        handler.handle(
            _cmd(dry_run=False),
            phase_results={EnumPhase.NIGHTLY_LOOP: False},
        )

        start_calls = bus.by_topic(TOPIC_OVERNIGHT_START)
        assert len(start_calls) == 1, "Self-loop must fire even on FAILED status"

    def test_self_loop_propagates_skip_flags(self) -> None:
        """Requeued command should carry the same skip flags as the original."""
        bus = _RecordingBus()
        handler = HandlerOvernight(event_bus=bus)

        handler.handle(_cmd(skip_merge_sweep=True, skip_nightly_loop=True))

        start_calls = bus.by_topic(TOPIC_OVERNIGHT_START)
        assert len(start_calls) == 1
        payload = start_calls[0]
        assert payload["skip_merge_sweep"] is True
        assert payload["skip_nightly_loop"] is True

    def test_default_loop_delay_is_300_seconds(self) -> None:
        """Default loop_delay_seconds must be 300 (5 minutes)."""
        cmd = ModelOvernightCommand(correlation_id="delay-default-test")
        assert cmd.loop_delay_seconds == 300

    def test_enable_self_loop_defaults_to_true(self) -> None:
        """enable_self_loop must default to True so the loop is on by default."""
        cmd = ModelOvernightCommand(correlation_id="self-loop-default-test")
        assert cmd.enable_self_loop is True
