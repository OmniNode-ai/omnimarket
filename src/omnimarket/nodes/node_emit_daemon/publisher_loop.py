# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Kafka publisher loop for the emit daemon.

Async background task: dequeue from BoundedEventQueue, publish to Kafka
via an injected publish_fn callable. Retry with exponential backoff,
drain on shutdown.

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
from datetime import datetime
from uuid import UUID, uuid4

from omnimarket.nodes.node_emit_daemon.event_queue import (
    BoundedEventQueue,
    ModelQueuedEvent,
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
    """Background publisher that dequeues events and publishes to Kafka.

    Args:
        queue: BoundedEventQueue to dequeue from.
        publish_fn: Async callable for publishing to Kafka.
        max_retry_attempts: Maximum retries before dropping an event.
        backoff_base_seconds: Base backoff for exponential retry.
        max_backoff_seconds: Cap on exponential backoff.
        source: Source identifier for event headers (e.g., "omniclaude").
    """

    def __init__(
        self,
        queue: BoundedEventQueue,
        publish_fn: PublishFn,
        max_retry_attempts: int = 3,
        backoff_base_seconds: float = 1.0,
        max_backoff_seconds: float = 60.0,
        source: str = "omnimarket",
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

        # Counters
        self.events_published: int = 0
        self.events_dropped: int = 0

    async def start(self) -> None:
        """Start the publisher loop as a background task."""
        self._running = True
        self._shutdown_event.clear()
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
            f"(published={self.events_published}, dropped={self.events_dropped})"
        )

    async def _loop(self) -> None:
        """Main publisher loop."""
        logger.info("Publisher loop running")

        while self._running:
            try:
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
                    self._retry_counts.pop(event.event_id, None)
                    self.events_published += 1
                else:
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
                            logger.error(f"Failed to re-enqueue event {event.event_id}")
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
