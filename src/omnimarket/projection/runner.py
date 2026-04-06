"""BaseProjectionRunner -- Kafka consumer lifecycle for projection nodes."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import signal
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from aiokafka import AIOKafkaConsumer  # type: ignore[import-untyped]

from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter
from omnimarket.projection.envelope import unwrap_envelope

logger = logging.getLogger(__name__)

KAFKA_BROKERS_ENV = "KAFKA_BROKERS"
DEFAULT_GROUP_ID = "omnimarket-projections-v1"
DEFAULT_CLIENT_ID = "omnimarket-projection"
RETRY_BASE_DELAY = 2.0
RETRY_MAX_DELAY = 30.0
MAX_RETRY_ATTEMPTS = 10


@dataclass
class MessageMeta:
    """Kafka message coordinates for deterministic dedup."""

    partition: int
    offset: int
    fallback_id: str


@dataclass
class ProjectionStats:
    """In-memory projection stats."""

    events_projected: int = 0
    errors_count: int = 0
    last_projected_at: datetime | None = None
    topic_stats: dict[str, dict[str, int]] = field(default_factory=dict)


def deterministic_correlation_id(topic: str, partition: int, offset: int) -> str:
    """Derive a deterministic UUID-shaped string from Kafka coordinates.

    Matches omnidash deterministicCorrelationId() exactly.
    """
    raw = f"{topic}:{partition}:{offset}"
    hex_digest = hashlib.sha256(raw.encode()).hexdigest()[:32]
    return f"{hex_digest[:8]}-{hex_digest[8:12]}-{hex_digest[12:16]}-{hex_digest[16:20]}-{hex_digest[20:32]}"


def safe_parse_date(value: Any) -> datetime:
    """Parse a date string, falling back to current wall-clock time."""
    if not value:
        return datetime.now(UTC)
    if isinstance(value, datetime):
        return value
    try:
        from dateutil.parser import isoparse  # type: ignore[import-untyped]

        dt: datetime = isoparse(str(value))
        return dt
    except Exception:
        pass
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt
    except (ValueError, TypeError):
        logger.warning(
            "safe_parse_date: malformed timestamp %r, using wall-clock", value
        )
        return datetime.now(UTC)


def safe_float(value: Any, default: float = 0.0) -> float:
    """Parse a float safely, returning default for non-finite values."""
    if value is None:
        return default
    try:
        f = float(value)
        if f != f:  # NaN check
            return default
        return f
    except (ValueError, TypeError):
        return default


def safe_int(value: Any, default: int = 0) -> int:
    """Parse an int safely."""
    if value is None:
        return default
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def coalesce(*values: Any) -> Any:
    """Return the first truthy value, or the last value."""
    for v in values:
        if v:
            return v
    return values[-1] if values else None


class BaseProjectionRunner(ABC):
    """Base class for Kafka->DB projection consumers.

    Subclasses implement:
    - topics: list of Kafka topics to subscribe to
    - project_event(topic, data, meta): project a single event to DB
    """

    def __init__(
        self,
        *,
        group_id: str | None = None,
        client_id: str | None = None,
    ) -> None:
        self._group_id = group_id or os.environ.get(
            "KAFKA_CONSUMER_GROUP", DEFAULT_GROUP_ID
        )
        self._client_id = client_id or DEFAULT_CLIENT_ID
        self._db = AsyncpgAdapter()
        self._stats = ProjectionStats()
        self._running = False
        self._consumer: AIOKafkaConsumer | None = None

    @property
    @abstractmethod
    def topics(self) -> list[str]:
        """Kafka topics this runner subscribes to."""
        ...

    @abstractmethod
    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        """Project a single event into the database.

        Returns True if projection succeeded, False if DB unavailable.
        """
        ...

    @property
    def db(self) -> AsyncpgAdapter:
        return self._db

    @property
    def stats(self) -> ProjectionStats:
        return self._stats

    async def run(self) -> None:
        """Main entry point -- connect to DB and Kafka, consume events."""
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: asyncio.ensure_future(self.shutdown()))

        await self._db.connect()
        logger.info("DB connected")

        brokers = os.environ.get(KAFKA_BROKERS_ENV, "localhost:9092")
        attempts = 0

        while attempts < MAX_RETRY_ATTEMPTS and self._running is not False:
            try:
                self._consumer = AIOKafkaConsumer(
                    *self.topics,
                    bootstrap_servers=brokers,
                    group_id=self._group_id,
                    client_id=self._client_id,
                    auto_offset_reset="earliest",
                    enable_auto_commit=True,
                    value_deserializer=None,
                )
                await self._consumer.start()
                self._running = True
                logger.info(
                    "Kafka consumer started. Topics: %s, Group: %s",
                    self.topics,
                    self._group_id,
                )

                async for msg in self._consumer:
                    if not self._running:
                        break
                    await self._handle_message(msg)

            except Exception as err:
                attempts += 1
                delay = min(RETRY_BASE_DELAY * (2**attempts), RETRY_MAX_DELAY)
                logger.error(
                    "Consumer attempt %d/%d failed: %s. Retrying in %.1fs",
                    attempts,
                    MAX_RETRY_ATTEMPTS,
                    err,
                    delay,
                )
                if self._consumer:
                    with contextlib.suppress(Exception):
                        await self._consumer.stop()
                    self._consumer = None
                await asyncio.sleep(delay)

        logger.error("Consumer failed after %d retries", MAX_RETRY_ATTEMPTS)
        await self._db.close()

    async def shutdown(self) -> None:
        """Graceful shutdown."""
        logger.info("Shutting down...")
        self._running = False
        if self._consumer:
            with contextlib.suppress(Exception):
                await self._consumer.stop()
        await self._db.close()

    async def _handle_message(self, msg: Any) -> None:
        """Parse, unwrap, dispatch, and track a single Kafka message."""
        topic = msg.topic
        try:
            if msg.value is None:
                return

            data = unwrap_envelope(msg.value)
            if data is None:
                return

            fallback_id = deterministic_correlation_id(topic, msg.partition, msg.offset)
            meta = MessageMeta(
                partition=msg.partition,
                offset=msg.offset,
                fallback_id=fallback_id,
            )

            projected = await self.project_event(topic, data, meta)

            if projected:
                self._stats.events_projected += 1
                self._stats.last_projected_at = datetime.now(UTC)
                ts = self._stats.topic_stats.setdefault(
                    topic, {"projected": 0, "errors": 0}
                )
                ts["projected"] += 1
                await self._update_watermark(f"{topic}:{msg.partition}", msg.offset)

        except Exception as err:
            self._stats.errors_count += 1
            ts = self._stats.topic_stats.setdefault(
                topic, {"projected": 0, "errors": 0}
            )
            ts["errors"] += 1
            logger.error("Error projecting %s: %s", topic, err)

    async def _update_watermark(self, projection_name: str, offset: int) -> None:
        """Update projection_watermarks table -- matches omnidash SQL exactly."""
        try:
            await self._db.execute(
                """
                INSERT INTO projection_watermarks (projection_name, last_offset, events_projected, updated_at)
                VALUES ($1, $2, 1, NOW())
                ON CONFLICT (projection_name) DO UPDATE SET
                  last_offset = GREATEST(projection_watermarks.last_offset, EXCLUDED.last_offset),
                  events_projected = projection_watermarks.events_projected + 1,
                  last_projected_at = NOW(), updated_at = NOW()
                """,
                projection_name,
                offset,
            )
        except Exception as err:
            logger.warning("Failed to update watermark: %s", err)
