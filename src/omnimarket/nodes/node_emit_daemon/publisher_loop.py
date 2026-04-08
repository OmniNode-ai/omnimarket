# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Kafka publisher loop with circuit breaker for the emit daemon.

Async background task: dequeue from BoundedEventQueue, publish to Kafka
via an injected publish_fn callable. Circuit breaker prevents hanging when
Kafka is down: after N consecutive failures the circuit opens, events are
buffered locally, and a recovery probe runs after a configurable timeout.

The publish_fn signature allows plugging in any Kafka client:
    async def publish_fn(topic: str, key: bytes | None, value: bytes, headers: dict) -> None

In standalone mode, publish_fn can be a no-op or local logger.
In kernel mode, publish_fn wraps EventBusKafka.publish().
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from uuid import UUID, uuid4

from omnimarket.nodes.node_emit_daemon.event_queue import (
    BoundedEventQueue,
    ModelQueuedEvent,
)
from omnimarket.nodes.node_emit_daemon.models.model_emit_daemon_config import (
    EnumCircuitBreakerState,
)

logger = logging.getLogger(__name__)

PUBLISHER_POLL_INTERVAL_SECONDS: float = 0.1

# Type for the injected publish function
PublishFn = Callable[
    [str, bytes | None, bytes, dict[str, str]],
    Awaitable[None],
]


def _json_default(obj: object) -> str:
    if isinstance(obj, UUID):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


class KafkaPublisherLoop:
    """Background publisher with circuit breaker that dequeues events and publishes to Kafka.

    Circuit breaker state machine:
        CLOSED  -- normal: publish events. On N consecutive failures -> OPEN.
        OPEN    -- Kafka down: buffer events in queue, wait recovery_timeout -> HALF_OPEN.
        HALF_OPEN -- probe: send one event. Success -> CLOSED. Failure -> OPEN.

    Args:
        queue: BoundedEventQueue to dequeue from.
        publish_fn: Async callable for publishing to Kafka.
        max_retry_attempts: Maximum retries before dropping an event.
        backoff_base_seconds: Base backoff for exponential retry.
        max_backoff_seconds: Cap on exponential backoff.
        source: Source identifier for event headers.
        failure_threshold: Consecutive failures before circuit opens.
        recovery_timeout: Seconds in OPEN before probing HALF_OPEN.
        half_open_max_probes: Successful probes in HALF_OPEN before closing.
    """

    def __init__(
        self,
        queue: BoundedEventQueue,
        publish_fn: PublishFn,
        max_retry_attempts: int = 3,
        backoff_base_seconds: float = 1.0,
        max_backoff_seconds: float = 60.0,
        source: str = "omnimarket",
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        half_open_max_probes: int = 1,
    ) -> None:
        self._queue = queue
        self._publish_fn = publish_fn
        self._max_retry_attempts = max_retry_attempts
        self._backoff_base_seconds = backoff_base_seconds
        self._max_backoff_seconds = max_backoff_seconds
        self._source = source

        self._running = False
        self._shutdown_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._retry_counts: dict[str, int] = {}

        # Circuit breaker state
        self._circuit_state = EnumCircuitBreakerState.CLOSED
        self._failure_threshold = failure_threshold
        self._recovery_timeout = recovery_timeout
        self._half_open_max_probes = half_open_max_probes
        self._consecutive_failures: int = 0
        self._half_open_successes: int = 0
        self._circuit_opened_at: datetime | None = None
        self._kafka_connected: bool = True  # True until first failure

        # Counters and timestamps
        self.events_published: int = 0
        self.events_dropped: int = 0
        self.events_buffered: int = 0
        self._last_publish_at: datetime | None = None
        self._last_failure_at: datetime | None = None
        self._started_at: datetime | None = None

    # ------------------------------------------------------------------
    # Circuit breaker properties (read by health endpoint)
    # ------------------------------------------------------------------

    @property
    def circuit_state(self) -> EnumCircuitBreakerState:
        return self._circuit_state

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    @property
    def circuit_opened_at(self) -> datetime | None:
        return self._circuit_opened_at

    @property
    def last_publish_at(self) -> datetime | None:
        return self._last_publish_at

    @property
    def last_failure_at(self) -> datetime | None:
        return self._last_failure_at

    @property
    def kafka_connected(self) -> bool:
        return self._kafka_connected

    @property
    def started_at(self) -> datetime | None:
        return self._started_at

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the publisher loop as a background task."""
        self._running = True
        self._shutdown_event.clear()
        self._started_at = datetime.now(UTC)
        self._task = asyncio.create_task(self._loop())
        logger.info("KafkaPublisherLoop started")

    async def stop(self, drain_timeout: float = 10.0) -> None:
        """Stop the publisher loop, waiting for graceful drain."""
        self._running = False
        self._shutdown_event.set()

        if self._task is not None:
            try:
                async with asyncio.timeout(drain_timeout):
                    await self._task
            except TimeoutError:
                logger.warning("Publisher loop drain timeout exceeded")
            except asyncio.CancelledError:
                pass
            self._task = None

        logger.info(
            f"KafkaPublisherLoop stopped "
            f"(published={self.events_published}, dropped={self.events_dropped}, "
            f"buffered={self.events_buffered}, circuit={self._circuit_state})"
        )

    # ------------------------------------------------------------------
    # Circuit breaker transitions
    # ------------------------------------------------------------------

    def _record_success(self) -> None:
        """Record a successful publish."""
        self._last_publish_at = datetime.now(UTC)
        self._kafka_connected = True

        if self._circuit_state == EnumCircuitBreakerState.HALF_OPEN:
            self._half_open_successes += 1
            if self._half_open_successes >= self._half_open_max_probes:
                self._transition_to_closed()
        elif self._circuit_state == EnumCircuitBreakerState.CLOSED:
            self._consecutive_failures = 0

    def _record_failure(self) -> None:
        """Record a failed publish."""
        self._consecutive_failures += 1
        self._last_failure_at = datetime.now(UTC)

        if self._circuit_state == EnumCircuitBreakerState.HALF_OPEN or (
            self._circuit_state == EnumCircuitBreakerState.CLOSED
            and self._consecutive_failures >= self._failure_threshold
        ):
            self._transition_to_open()

    def _transition_to_open(self) -> None:
        """Open the circuit -- stop publishing, buffer events."""
        self._circuit_state = EnumCircuitBreakerState.OPEN
        self._circuit_opened_at = datetime.now(UTC)
        self._kafka_connected = False
        logger.warning(
            f"Circuit breaker OPEN after {self._consecutive_failures} consecutive "
            f"failures. Events will be buffered for {self._recovery_timeout}s."
        )

    def _transition_to_half_open(self) -> None:
        """Probe Kafka with a single event."""
        self._circuit_state = EnumCircuitBreakerState.HALF_OPEN
        self._half_open_successes = 0
        logger.info("Circuit breaker HALF_OPEN -- probing Kafka")

    def _transition_to_closed(self) -> None:
        """Close the circuit -- resume normal publishing."""
        self._circuit_state = EnumCircuitBreakerState.CLOSED
        self._consecutive_failures = 0
        self._half_open_successes = 0
        self._circuit_opened_at = None
        self._kafka_connected = True
        logger.info("Circuit breaker CLOSED -- Kafka recovered, resuming publishing")

    def _should_probe(self) -> bool:
        """Check if enough time has passed in OPEN state to probe."""
        if self._circuit_state != EnumCircuitBreakerState.OPEN:
            return False
        if self._circuit_opened_at is None:
            return True
        elapsed = (datetime.now(UTC) - self._circuit_opened_at).total_seconds()
        return elapsed >= self._recovery_timeout

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def _loop(self) -> None:
        """Main publisher loop with circuit breaker."""
        logger.info("Publisher loop running")

        while self._running:
            try:
                # When circuit is OPEN, wait for recovery timeout before probing
                if self._circuit_state == EnumCircuitBreakerState.OPEN:
                    if not self._should_probe():
                        # Still in cooldown -- sleep briefly and re-check
                        try:
                            await asyncio.wait_for(
                                self._shutdown_event.wait(),
                                timeout=min(1.0, self._recovery_timeout),
                            )
                            break  # Shutdown requested
                        except TimeoutError:
                            continue
                    # Recovery timeout elapsed -- transition to HALF_OPEN
                    self._transition_to_half_open()

                event = await self._queue.dequeue()

                if event is None:
                    try:
                        await asyncio.wait_for(
                            self._shutdown_event.wait(),
                            timeout=PUBLISHER_POLL_INTERVAL_SECONDS,
                        )
                        break  # Shutdown requested
                    except TimeoutError:
                        continue

                success = await self._publish_event(event)

                if success:
                    self._record_success()
                    self._retry_counts.pop(event.event_id, None)
                    self.events_published += 1
                else:
                    self._record_failure()
                    retries = self._retry_counts.get(event.event_id, 0) + 1
                    self._retry_counts[event.event_id] = retries

                    if retries >= self._max_retry_attempts:
                        logger.error(
                            f"Dropping event {event.event_id} after {retries} retries",
                            extra={
                                "event_type": event.event_type,
                                "topic": event.topic,
                            },
                        )
                        self._retry_counts.pop(event.event_id, None)
                        self.events_dropped += 1
                    else:
                        # Re-enqueue for retry. If circuit just opened, events
                        # accumulate in the queue (buffered) until Kafka recovers.
                        if self._circuit_state == EnumCircuitBreakerState.OPEN:
                            # Buffer the event -- don't retry immediately
                            requeue_success = await self._queue.enqueue(event)
                            if requeue_success:
                                self.events_buffered += 1
                            else:
                                logger.error(f"Failed to buffer event {event.event_id}")
                                self._retry_counts.pop(event.event_id, None)
                                self.events_dropped += 1
                        else:
                            uncapped_backoff = self._backoff_base_seconds * (
                                2 ** (retries - 1)
                            )
                            backoff = min(uncapped_backoff, self._max_backoff_seconds)
                            logger.warning(
                                f"Publish failed for {event.event_id}, "
                                f"retry {retries}/{self._max_retry_attempts} "
                                f"in {backoff}s",
                            )
                            try:
                                await asyncio.wait_for(
                                    self._shutdown_event.wait(), timeout=backoff
                                )
                                # Shutdown during backoff -- re-enqueue and exit
                                await self._queue.enqueue(event)
                                break
                            except TimeoutError:
                                pass  # Normal backoff completed

                            requeue_success = await self._queue.enqueue(event)
                            if not requeue_success:
                                logger.error(
                                    f"Failed to re-enqueue event {event.event_id}"
                                )
                                self._retry_counts.pop(event.event_id, None)
                                self.events_dropped += 1

            except asyncio.CancelledError:
                logger.info("Publisher loop cancelled")
                break
            except Exception:
                logger.exception("Unexpected error in publisher loop")
                await asyncio.sleep(1.0)

        logger.info("Publisher loop stopped")

    async def _publish_event(self, event: ModelQueuedEvent) -> bool:
        """Publish a single event via the injected publish_fn."""
        try:
            key = event.partition_key.encode("utf-8") if event.partition_key else None
            value = json.dumps(event.payload, default=_json_default).encode("utf-8")

            # Build correlation ID from payload
            payload_corr = (
                event.payload.get("correlation_id")
                if isinstance(event.payload, dict)
                else None
            )
            if isinstance(payload_corr, str):
                try:
                    correlation_id = str(UUID(payload_corr))
                except ValueError:
                    correlation_id = str(uuid4())
            else:
                correlation_id = str(uuid4())

            headers = {
                "source": self._source,
                "event_type": event.event_type,
                "timestamp": event.queued_at.isoformat(),
                "correlation_id": correlation_id,
            }

            await self._publish_fn(event.topic, key, value, headers)

            logger.debug(
                f"Published event {event.event_id}",
                extra={
                    "event_type": event.event_type,
                    "topic": event.topic,
                },
            )
            return True

        except Exception as e:
            logger.warning(
                f"Failed to publish event {event.event_id}: {e}",
                extra={
                    "event_type": event.event_type,
                    "topic": event.topic,
                },
            )
            return False


__all__: list[str] = ["KafkaPublisherLoop", "PublishFn"]
