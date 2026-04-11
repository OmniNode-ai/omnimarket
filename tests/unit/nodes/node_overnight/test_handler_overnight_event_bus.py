"""Unit tests for HandlerOvernight event bus publishing (OMN-8405).

Covers the ``event_bus`` injection seam: phase-start + phase-end envelopes
fire around each non-skipped phase and a single session-complete envelope
fires at end of run. Skipped phases must not emit phase-start/end events.
"""

from __future__ import annotations

import json
from typing import Any

from omnimarket.nodes.node_overnight.handlers.handler_overnight import (
    EnumPhase,
    HandlerOvernight,
    ModelOvernightCommand,
)
from omnimarket.nodes.node_overnight.topics import (
    TOPIC_OVERNIGHT_COMPLETE,
    TOPIC_OVERNIGHT_PHASE_END,
    TOPIC_OVERNIGHT_PHASE_START,
)


class _RecordingBus:
    """Sync event-bus stub that captures (topic, payload_dict) tuples."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, topic: str, payload: bytes) -> None:
        self.calls.append((topic, json.loads(payload.decode())))


def _cmd(**overrides: Any) -> ModelOvernightCommand:
    defaults: dict[str, Any] = {
        "correlation_id": "test-overnight-8405",
        "dry_run": True,
    }
    defaults.update(overrides)
    return ModelOvernightCommand(**defaults)


def test_publishes_phase_start_end_and_complete_for_every_non_skipped_phase() -> None:
    bus = _RecordingBus()
    handler = HandlerOvernight(event_bus=bus)

    handler.handle(_cmd())

    # 5 phases x (start + end) + 1 complete = 11 envelopes
    assert len(bus.calls) == 11, [c[0] for c in bus.calls]

    topics = [c[0] for c in bus.calls]
    assert topics.count(TOPIC_OVERNIGHT_PHASE_START) == 5
    assert topics.count(TOPIC_OVERNIGHT_PHASE_END) == 5
    assert topics.count(TOPIC_OVERNIGHT_COMPLETE) == 1
    assert topics[-1] == TOPIC_OVERNIGHT_COMPLETE


def test_phase_start_precedes_phase_end_for_same_phase() -> None:
    bus = _RecordingBus()
    handler = HandlerOvernight(event_bus=bus)

    handler.handle(_cmd())

    # Pair-match: every time a phase-start fires, the very next call for that
    # phase must be its phase-end (skips/reorderings would break this).
    order: list[tuple[str, str]] = [
        (c[0], c[1]["phase"]) for c in bus.calls if c[0] != TOPIC_OVERNIGHT_COMPLETE
    ]
    for i in range(0, len(order), 2):
        start_topic, start_phase = order[i]
        end_topic, end_phase = order[i + 1]
        assert start_topic == TOPIC_OVERNIGHT_PHASE_START
        assert end_topic == TOPIC_OVERNIGHT_PHASE_END
        assert start_phase == end_phase


def test_skipped_phases_emit_no_phase_envelopes() -> None:
    bus = _RecordingBus()
    handler = HandlerOvernight(event_bus=bus)

    handler.handle(
        _cmd(
            skip_nightly_loop=True,
            skip_build_loop=True,
            skip_merge_sweep=True,
        )
    )

    phase_events = [c for c in bus.calls if c[0] != TOPIC_OVERNIGHT_COMPLETE]
    emitted_phases = {c[1]["phase"] for c in phase_events}
    assert EnumPhase.NIGHTLY_LOOP.value not in emitted_phases
    assert EnumPhase.BUILD_LOOP.value not in emitted_phases
    assert EnumPhase.MERGE_SWEEP.value not in emitted_phases
    # ci_watch + platform_readiness still run, so each emits start+end = 4 events
    assert len(phase_events) == 4


def test_phase_end_envelope_carries_phase_status_and_duration() -> None:
    bus = _RecordingBus()
    handler = HandlerOvernight(event_bus=bus)

    handler.handle(
        _cmd(dry_run=False),
        phase_results={
            EnumPhase.NIGHTLY_LOOP: True,
            EnumPhase.BUILD_LOOP: False,
            EnumPhase.MERGE_SWEEP: True,
            EnumPhase.CI_WATCH: True,
            EnumPhase.PLATFORM_READINESS: True,
        },
    )

    end_payloads = {
        c[1]["phase"]: c[1] for c in bus.calls if c[0] == TOPIC_OVERNIGHT_PHASE_END
    }
    assert end_payloads[EnumPhase.NIGHTLY_LOOP.value]["phase_status"] == "success"
    assert end_payloads[EnumPhase.BUILD_LOOP.value]["phase_status"] == "failed"
    assert end_payloads[EnumPhase.BUILD_LOOP.value]["error_message"] is not None

    for payload in end_payloads.values():
        assert "duration_ms" in payload
        assert isinstance(payload["duration_ms"], int)
        assert payload["duration_ms"] >= 0


def test_complete_envelope_records_session_status_and_phase_totals() -> None:
    bus = _RecordingBus()
    handler = HandlerOvernight(event_bus=bus)

    handler.handle(
        _cmd(skip_merge_sweep=True),
    )

    complete_calls = [c for c in bus.calls if c[0] == TOPIC_OVERNIGHT_COMPLETE]
    assert len(complete_calls) == 1
    payload = complete_calls[0][1]
    assert payload["correlation_id"] == "test-overnight-8405"
    assert payload["session_status"] == "completed"
    assert EnumPhase.MERGE_SWEEP.value in payload["phases_skipped"]
    assert EnumPhase.NIGHTLY_LOOP.value in payload["phases_run"]
    assert payload["halt_reason"] is None


def test_no_event_bus_is_a_noop() -> None:
    handler = HandlerOvernight()  # event_bus=None (default)

    # Must not raise; must still return a valid result.
    result = handler.handle(_cmd())
    assert result.session_status.value == "completed"


def test_publish_failure_does_not_break_pipeline() -> None:
    def _boom(topic: str, payload: bytes) -> None:
        raise RuntimeError(f"bus down: {topic}")

    handler = HandlerOvernight(event_bus=_boom)

    result = handler.handle(_cmd())
    assert result.session_status.value == "completed"
