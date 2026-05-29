# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Focused unit tests for KafkaPublisherLoop circuit breaker and retry behavior.

Covers:
- Circuit breaker transitions: CLOSED -> OPEN -> HALF_OPEN -> CLOSED
- _record_success / _record_failure state machine
- _should_probe timing logic
- _publish_event success returns True, failure returns False
- Retry counter increments and event dropping after max_retry_attempts
- events_published / events_dropped / events_buffered counters
- stop() drain path (not a full integration, just lifecycle)

Related: OMN-12385
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from omnimarket.nodes.node_emit_daemon.event_queue import (
    BoundedEventQueue,
    ModelQueuedEvent,
)
from omnimarket.nodes.node_emit_daemon.models.model_emit_daemon_config import (
    EnumCircuitBreakerState,
)
from omnimarket.nodes.node_emit_daemon.publisher_loop import KafkaPublisherLoop

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(
    event_id: str = "evt-1", topic: str = "onex.evt.test.topic.v1"
) -> ModelQueuedEvent:
    return ModelQueuedEvent(
        event_id=event_id,
        event_type="test.event",
        topic=topic,
        payload={"key": "value"},
        queued_at=datetime.now(UTC),
    )


def _make_loop(
    queue: BoundedEventQueue | None = None,
    publish_fn: AsyncMock | None = None,
    *,
    max_retry_attempts: int = 3,
    failure_threshold: int = 3,
    recovery_timeout: float = 30.0,
    half_open_max_probes: int = 1,
) -> KafkaPublisherLoop:
    if queue is None:
        queue = BoundedEventQueue(max_memory_queue=100)
    if publish_fn is None:
        publish_fn = AsyncMock()
    return KafkaPublisherLoop(
        queue=queue,
        publish_fn=publish_fn,
        max_retry_attempts=max_retry_attempts,
        backoff_base_seconds=0.0,
        max_backoff_seconds=0.0,
        source="test",
        failure_threshold=failure_threshold,
        recovery_timeout=recovery_timeout,
        half_open_max_probes=half_open_max_probes,
    )


# ---------------------------------------------------------------------------
# Circuit breaker state machine: unit-level (no I/O)
# ---------------------------------------------------------------------------


class TestCircuitBreakerStateMachine:
    """Test _record_success / _record_failure state transitions directly."""

    def test_initial_state_is_closed(self) -> None:
        loop = _make_loop()
        assert loop.circuit_state == EnumCircuitBreakerState.CLOSED
        assert loop.consecutive_failures == 0
        assert loop.kafka_connected is True

    def test_failures_below_threshold_stay_closed(self) -> None:
        loop = _make_loop(failure_threshold=3)
        loop._record_failure()
        loop._record_failure()
        assert loop.circuit_state == EnumCircuitBreakerState.CLOSED
        assert loop.consecutive_failures == 2

    def test_failures_at_threshold_open_circuit(self) -> None:
        loop = _make_loop(failure_threshold=3)
        loop._record_failure()
        loop._record_failure()
        loop._record_failure()
        assert loop.circuit_state == EnumCircuitBreakerState.OPEN
        assert loop.kafka_connected is False
        assert loop.circuit_opened_at is not None

    def test_success_in_closed_resets_failure_count(self) -> None:
        loop = _make_loop(failure_threshold=5)
        loop._record_failure()
        loop._record_failure()
        assert loop.consecutive_failures == 2
        loop._record_success()
        assert loop.consecutive_failures == 0
        assert loop.circuit_state == EnumCircuitBreakerState.CLOSED

    def test_success_in_half_open_transitions_to_closed(self) -> None:
        loop = _make_loop(failure_threshold=1, half_open_max_probes=1)
        # Force to HALF_OPEN
        loop._transition_to_open()
        loop._transition_to_half_open()
        assert loop.circuit_state == EnumCircuitBreakerState.HALF_OPEN
        loop._record_success()
        assert loop.circuit_state == EnumCircuitBreakerState.CLOSED
        assert loop.kafka_connected is True
        assert loop.circuit_opened_at is None

    def test_failure_in_half_open_transitions_back_to_open(self) -> None:
        loop = _make_loop(failure_threshold=1)
        loop._transition_to_open()
        loop._transition_to_half_open()
        assert loop.circuit_state == EnumCircuitBreakerState.HALF_OPEN
        loop._record_failure()
        assert loop.circuit_state == EnumCircuitBreakerState.OPEN

    def test_half_open_requires_multiple_probes_when_configured(self) -> None:
        loop = _make_loop(failure_threshold=1, half_open_max_probes=2)
        loop._transition_to_open()
        loop._transition_to_half_open()
        loop._record_success()
        # Only 1 success, need 2 -> should still be HALF_OPEN
        assert loop.circuit_state == EnumCircuitBreakerState.HALF_OPEN
        loop._record_success()
        # Now 2 successes -> CLOSED
        assert loop.circuit_state == EnumCircuitBreakerState.CLOSED

    def test_transition_to_closed_resets_all_counters(self) -> None:
        loop = _make_loop(failure_threshold=2)
        loop._record_failure()
        loop._record_failure()
        loop._transition_to_half_open()
        loop._transition_to_closed()
        assert loop.circuit_state == EnumCircuitBreakerState.CLOSED
        assert loop.consecutive_failures == 0
        assert loop._half_open_successes == 0
        assert loop.circuit_opened_at is None
        assert loop.kafka_connected is True


# ---------------------------------------------------------------------------
# _should_probe timing
# ---------------------------------------------------------------------------


class TestShouldProbe:
    def test_closed_circuit_never_probes(self) -> None:
        loop = _make_loop()
        assert loop._should_probe() is False

    def test_open_circuit_with_recent_failure_does_not_probe(self) -> None:
        loop = _make_loop(recovery_timeout=30.0)
        loop._transition_to_open()
        # opened_at is now — not enough time has passed
        assert loop._should_probe() is False

    def test_open_circuit_after_timeout_should_probe(self) -> None:
        loop = _make_loop(recovery_timeout=1.0)
        loop._transition_to_open()
        # Backdate circuit_opened_at to simulate elapsed time
        loop._circuit_opened_at = datetime.now(UTC) - timedelta(seconds=2)
        assert loop._should_probe() is True

    def test_open_circuit_with_none_opened_at_always_probes(self) -> None:
        loop = _make_loop(recovery_timeout=30.0)
        loop._circuit_state = EnumCircuitBreakerState.OPEN
        loop._circuit_opened_at = None
        assert loop._should_probe() is True

    def test_half_open_circuit_does_not_probe(self) -> None:
        loop = _make_loop(recovery_timeout=1.0)
        loop._transition_to_open()
        loop._transition_to_half_open()
        assert loop._should_probe() is False


# ---------------------------------------------------------------------------
# _publish_event: success/failure return values
# ---------------------------------------------------------------------------


class TestPublishEvent:
    @pytest.mark.asyncio
    async def test_publish_event_success_returns_true(self) -> None:
        publish_fn = AsyncMock()
        loop = _make_loop(publish_fn=publish_fn)
        event = _make_event()
        result = await loop._publish_event(event)
        assert result is True
        publish_fn.assert_called_once()

    @pytest.mark.asyncio
    async def test_publish_event_failure_returns_false(self) -> None:
        publish_fn = AsyncMock(side_effect=Exception("broker down"))
        loop = _make_loop(publish_fn=publish_fn)
        event = _make_event()
        result = await loop._publish_event(event)
        assert result is False

    @pytest.mark.asyncio
    async def test_publish_event_passes_correct_topic(self) -> None:
        calls: list[str] = []

        async def capture_fn(
            topic: str, key: object, value: object, headers: object
        ) -> None:
            calls.append(topic)

        loop = _make_loop(publish_fn=capture_fn)  # type: ignore[arg-type]
        event = _make_event(topic="onex.evt.omnimarket.custom.v1")
        await loop._publish_event(event)
        assert calls == ["onex.evt.omnimarket.custom.v1"]

    @pytest.mark.asyncio
    async def test_publish_event_encodes_payload_as_json(self) -> None:
        import json

        payloads: list[bytes] = []

        async def capture_fn(
            topic: str, key: object, value: bytes, headers: object
        ) -> None:
            payloads.append(value)

        loop = _make_loop(publish_fn=capture_fn)  # type: ignore[arg-type]
        event = _make_event()
        await loop._publish_event(event)
        decoded = json.loads(payloads[0])
        assert decoded == {"key": "value"}

    @pytest.mark.asyncio
    async def test_publish_event_with_partition_key_encodes_key(self) -> None:
        keys: list[bytes | None] = []

        async def capture_fn(
            topic: str, key: bytes | None, value: bytes, headers: object
        ) -> None:
            keys.append(key)

        loop = _make_loop(publish_fn=capture_fn)  # type: ignore[arg-type]
        event = ModelQueuedEvent(
            event_id="evt-pk",
            event_type="test",
            topic="onex.evt.test.v1",
            payload={},
            partition_key="partition-abc",
            queued_at=datetime.now(UTC),
        )
        await loop._publish_event(event)
        assert keys[0] == b"partition-abc"

    @pytest.mark.asyncio
    async def test_publish_event_no_partition_key_sends_none_key(self) -> None:
        keys: list[bytes | None] = []

        async def capture_fn(
            topic: str, key: bytes | None, value: bytes, headers: object
        ) -> None:
            keys.append(key)

        loop = _make_loop(publish_fn=capture_fn)  # type: ignore[arg-type]
        event = _make_event()  # no partition_key
        await loop._publish_event(event)
        assert keys[0] is None


# ---------------------------------------------------------------------------
# Counter increments via the main _loop (driven by artificial queue)
# ---------------------------------------------------------------------------


class TestPublisherLoopCounters:
    """Drive _loop by starting the loop and enqueuing events."""

    @pytest.mark.asyncio
    async def test_successful_publish_increments_events_published(self) -> None:
        publish_fn = AsyncMock()
        queue: BoundedEventQueue = BoundedEventQueue(max_memory_queue=10)
        loop = _make_loop(queue=queue, publish_fn=publish_fn, max_retry_attempts=0)

        await queue.enqueue(_make_event("e1"))
        await loop.start()
        # Give the loop a tick to process
        await asyncio.sleep(0.05)
        await loop.stop(drain_timeout=1.0)

        assert loop.events_published >= 1

    @pytest.mark.asyncio
    async def test_failed_publish_past_max_retries_increments_dropped(self) -> None:
        publish_fn = AsyncMock(side_effect=Exception("fail"))
        queue: BoundedEventQueue = BoundedEventQueue(max_memory_queue=10)
        loop = _make_loop(
            queue=queue,
            publish_fn=publish_fn,
            max_retry_attempts=0,
            failure_threshold=10,  # keep circuit closed
        )

        await queue.enqueue(_make_event("e1"))
        await loop.start()
        await asyncio.sleep(0.1)
        await loop.stop(drain_timeout=1.0)

        assert loop.events_dropped >= 1

    @pytest.mark.asyncio
    async def test_start_sets_started_at(self) -> None:
        loop = _make_loop()
        assert loop.started_at is None
        await loop.start()
        assert loop.started_at is not None
        await loop.stop()

    @pytest.mark.asyncio
    async def test_stop_is_idempotent(self) -> None:
        loop = _make_loop()
        await loop.start()
        await loop.stop()
        await loop.stop()  # second stop must not raise
