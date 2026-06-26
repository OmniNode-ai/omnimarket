"""BaseProjectionRunner -- Kafka consumer lifecycle for projection nodes."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import signal
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from aiokafka import AIOKafkaConsumer, TopicPartition
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)

from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter
from omnimarket.config.settings import Settings
from omnimarket.projection.dlq import (
    build_dlq_envelope,
    correlation_id_from_payload,
)
from omnimarket.projection.envelope import unwrap_envelope
from omnimarket.projection.error_classification import (
    ProjectionErrorClass,
    classify_projection_error,
)

logger = logging.getLogger(__name__)

KAFKA_BROKERS_ENV = "KAFKA_BROKERS"
PROJECTION_RUNTIME_BINDING_OVERLAY_ENV = "OMNIMARKET_PROJECTION_RUNTIME_BINDING_OVERLAY"
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


class ModelProjectionRuntimeBinding(BaseModel):
    """Runtime binding for Kafka-backed projection consumers.

    Demo and judge paths should supply this from a contract overlay. Legacy env
    resolution remains only in ``from_legacy_settings`` for existing local
    scripts that have not migrated yet.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kafka_bootstrap_servers: str = Field(
        description="Kafka broker list used by the projection consumer and producer."
    )
    kafka_consumer_group: str = Field(default=DEFAULT_GROUP_ID)
    kafka_client_id: str = Field(default=DEFAULT_CLIENT_ID)
    database_url: SecretStr | None = Field(default=None)
    database_url_secret_ref: str | None = Field(default=None)
    source: str = Field(default="explicit")

    @field_validator(
        "kafka_bootstrap_servers", "kafka_consumer_group", "kafka_client_id"
    )
    @classmethod
    def _required_strings_non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("projection runtime binding value must be non-empty")
        return value.strip()

    @field_validator("database_url")
    @classmethod
    def _database_url_non_empty(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and not value.get_secret_value().strip():
            raise ValueError("projection database_url must be non-empty")
        return value

    @field_validator("database_url_secret_ref")
    @classmethod
    def _database_url_secret_ref_non_empty(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("projection database_url_secret_ref must be non-empty")
        return value.strip() if value is not None else None

    @model_validator(mode="after")
    def _exactly_one_database_source(self) -> ModelProjectionRuntimeBinding:
        if bool(self.database_url) == bool(self.database_url_secret_ref):
            raise ValueError(
                "declare exactly one of database_url or database_url_secret_ref"
            )
        return self

    @classmethod
    def from_legacy_settings(
        cls, settings: Settings | None = None
    ) -> ModelProjectionRuntimeBinding:
        resolved = settings or Settings()
        brokers = os.environ.get(KAFKA_BROKERS_ENV, "").strip()
        if not brokers:
            brokers = (
                resolved.kafka_bootstrap_servers.strip()
                or resolved.kafka_broker.strip()
            )
        for candidate in (
            resolved.omnidash_analytics_db_url,
            resolved.omnibase_infra_db_url,
        ):
            if candidate.get_secret_value().strip():
                return cls(
                    kafka_bootstrap_servers=brokers,
                    kafka_consumer_group=(
                        os.environ.get("KAFKA_CONSUMER_GROUP", "").strip()
                        or resolved.kafka_consumer_group.strip()
                        or DEFAULT_GROUP_ID
                    ),
                    kafka_client_id=DEFAULT_CLIENT_ID,
                    database_url=candidate,
                    source="legacy-settings",
                )
        raise RuntimeError("legacy projection runtime Settings are incomplete")

    def resolve_database_url(self) -> str:
        if self.database_url is not None:
            return self.database_url.get_secret_value()
        return _resolve_database_url_secret_ref(self.database_url_secret_ref)


def _resolve_database_url_secret_ref(secret_ref: str | None) -> str:
    if secret_ref is None:
        raise RuntimeError("projection database_url_secret_ref is required")
    ref = secret_ref.strip()
    if ref.startswith("env:"):
        env_name = ref.removeprefix("env:").strip()
        if not env_name:
            raise RuntimeError("projection database_url_secret_ref env name is empty")
        value = os.environ.get(env_name, "").strip()
        if not value:
            raise RuntimeError(f"projection database secret ref {ref!r} is unresolved")
        return value
    if ref.startswith("settings:"):
        field_name = ref.removeprefix("settings:").strip()
        setting_value = getattr(Settings(), field_name, None)
        if (
            isinstance(setting_value, SecretStr)
            and setting_value.get_secret_value().strip()
        ):
            return setting_value.get_secret_value()
        raise RuntimeError(f"projection database secret ref {ref!r} is unresolved")
    raise RuntimeError(
        "unsupported projection database_url_secret_ref; use env:<NAME> as a "
        "temporary secret boundary or settings:<field_name>"
    )


def load_projection_runtime_binding_overlay(
    path: str | Path,
) -> ModelProjectionRuntimeBinding:
    overlay_path = Path(path)
    raw = yaml.safe_load(overlay_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise RuntimeError(
            f"projection runtime binding overlay must be a mapping: {overlay_path}"
        )
    binding = ModelProjectionRuntimeBinding.model_validate(raw)
    return binding.model_copy(update={"source": f"overlay:{overlay_path}"})


def _projection_runtime_binding_from_overlay_env() -> (
    ModelProjectionRuntimeBinding | None
):
    overlay_path = os.environ.get(PROJECTION_RUNTIME_BINDING_OVERLAY_ENV, "").strip()
    if not overlay_path:
        return None
    return load_projection_runtime_binding_overlay(overlay_path)


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
        runtime_binding: ModelProjectionRuntimeBinding | None = None,
        runtime_binding_overlay_path: str | Path | None = None,
    ) -> None:
        resolved_binding = runtime_binding
        if resolved_binding is None and runtime_binding_overlay_path is not None:
            resolved_binding = load_projection_runtime_binding_overlay(
                runtime_binding_overlay_path
            )
        if resolved_binding is None:
            resolved_binding = _projection_runtime_binding_from_overlay_env()
        if resolved_binding is None:
            with contextlib.suppress(RuntimeError, ValidationError):
                resolved_binding = ModelProjectionRuntimeBinding.from_legacy_settings()
        self._runtime_binding = resolved_binding
        self._group_id = group_id or (
            resolved_binding.kafka_consumer_group
            if resolved_binding is not None
            else os.environ.get("KAFKA_CONSUMER_GROUP", DEFAULT_GROUP_ID)
        )
        self._client_id = client_id or (
            resolved_binding.kafka_client_id
            if resolved_binding is not None
            else DEFAULT_CLIENT_ID
        )
        self._db = AsyncpgAdapter(
            dsn=(
                resolved_binding.resolve_database_url()
                if resolved_binding is not None
                else None
            )
        )
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
    def poison_dlq_topics(self) -> list[str]:
        """Contract-declared DLQ topic(s) for POISON (malformed) events.

        OMN-13634: a subclass that consumes inbound bus events overrides this to
        return its ``event_bus.dlq_topics``. The default empty list means a
        subclass declared no DLQ; an uncatchable POISON error then re-raises
        loudly (fail-loud) rather than being silently dropped.
        """
        return []

    async def publish_dlq(self, topic: str, value: bytes) -> None:
        """Publish a DLQ envelope. Default no-op; subclasses override.

        OMN-13634: a subclass with a Kafka producer overrides this so the unified
        classifier in ``_handle_message`` can route a POISON event to the
        contract-declared DLQ topic. The default is a no-op so the base class
        stays usable in tests without a producer.
        """
        return

    @property
    def db(self) -> AsyncpgAdapter:
        return self._db

    @property
    def kafka_bootstrap_servers(self) -> str:
        if self._runtime_binding is not None:
            return self._runtime_binding.kafka_bootstrap_servers
        settings = Settings()
        return (
            os.environ.get(KAFKA_BROKERS_ENV, "").strip()
            or settings.kafka_bootstrap_servers.strip()
            or settings.kafka_broker.strip()
        )

    @property
    def runtime_binding_source(self) -> str:
        return (
            self._runtime_binding.source
            if self._runtime_binding is not None
            else "legacy-env-settings"
        )

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

        brokers = self.kafka_bootstrap_servers
        if not brokers:
            raise RuntimeError(
                "projection Kafka bootstrap servers are required; provide "
                "ModelProjectionRuntimeBinding or a projection runtime binding overlay"
            )
        attempts = 0

        while attempts < MAX_RETRY_ATTEMPTS and self._running is not False:
            try:
                self._consumer = AIOKafkaConsumer(
                    *self.topics,
                    bootstrap_servers=brokers,
                    group_id=self._group_id,
                    client_id=self._client_id,
                    auto_offset_reset="earliest",
                    # OMN-13350 (fail-loud): auto-commit was the data-loss
                    # mechanism. When enabled, the offset advanced on the timer
                    # regardless of whether the DB write succeeded, so a failed
                    # INSERT (e.g. UndefinedColumn on a schema-drifted projection
                    # table) was committed-and-dropped while the group reported
                    # Stable / LAG=0. The offset is now committed explicitly ONLY
                    # after a row is successfully projected (or the message is a
                    # genuine non-event); a projection error propagates and leaves
                    # the offset uncommitted so the message is re-read, never
                    # silently skipped.
                    enable_auto_commit=False,
                    value_deserializer=None,
                )
                await self._consumer.start()
                self._running = True
                logger.info(
                    "Kafka consumer started. Topics: %s, Group: %s, Binding: %s",
                    self.topics,
                    self._group_id,
                    self.runtime_binding_source,
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
        """Parse, unwrap, dispatch, and commit a single Kafka message.

        Fail-loud contract (OMN-13350): the consumer runs with auto-commit
        disabled. This method commits the offset ONLY when the message is fully
        accounted for — either successfully projected, or a genuine non-event
        (empty value / un-unwrappable envelope) that carries no row to persist.

        Error classification (OMN-13634 / WS-F Phase 2): a projection error is
        classified via :func:`classify_projection_error` so POISON and
        RECOVERABLE failures get the right policy — the same policy the infra
        ``handler_wiring`` path applies:

        * RECOVERABLE (e.g. an ``UndefinedColumn`` from a not-yet-applied
          migration, an ``OperationalError``, a dropped connection) is counted,
          logged, and **re-raised** so the offset is NOT committed. The outer run
          loop restarts the consumer and the uncommitted message is re-read on
          the next poll — the failure surfaces loudly and the data is never
          committed-and-dropped while the group reports Stable. A migration gap
          lands here, never quarantined as malformed.
        * POISON (a malformed payload ``ValidationError`` / ``PoisonEventError``
          that will never project no matter how often it is retried) is routed to
          the contract-declared poison DLQ and the offset IS committed, so it is
          captured durably (recoverable by correlation_id) instead of retried in
          a hot loop forever.

        Most handlers catch their own ``ValidationError`` inside ``project_event``
        and route to the DLQ before returning (the OMN-13548 path); this method
        is the safety net for a POISON error that escapes a handler and the
        re-read/no-commit guarantee for every RECOVERABLE error.
        """
        topic = msg.topic
        # Bound before the try so the except block always has a dict to attach to
        # a DLQ envelope, even if a poison error somehow surfaces before
        # project_event (it does not today — handlers raise inside project_event).
        data: dict[str, Any] = {}
        try:
            if msg.value is None:
                await self._commit_message(msg)
                return

            unwrapped = unwrap_envelope(msg.value)
            if unwrapped is None:
                await self._commit_message(msg)
                return
            data = unwrapped

            fallback_id = deterministic_correlation_id(topic, msg.partition, msg.offset)
            meta = MessageMeta(
                partition=msg.partition,
                offset=msg.offset,
                fallback_id=fallback_id,
            )

            projected = await self.project_event(topic, data, meta)
        except Exception as err:
            # OMN-13634: classify before deciding offset policy.
            self._stats.errors_count += 1
            ts = self._stats.topic_stats.setdefault(
                topic, {"projected": 0, "errors": 0}
            )
            ts["errors"] += 1
            error_class = classify_projection_error(err)
            if error_class is ProjectionErrorClass.POISON:
                # The payload is bad and will never project. Route it to the
                # poison DLQ (durably recoverable by correlation_id) and commit so
                # it is not retried in a hot loop forever.
                routed = await self._route_poison_to_dlq(topic, data, err, meta)
                logger.error(
                    "POISON event on %s routed to DLQ=%s (offset committed): %s",
                    topic,
                    routed,
                    err,
                )
                await self._commit_message(msg)
                return
            # RECOVERABLE: a missing column / server error / dropped connection.
            # Do NOT advance the offset; re-raise so the run loop tears the
            # consumer down with the offset uncommitted; the message is re-read,
            # never dropped, until the infra catches up.
            logger.error(
                "RECOVERABLE error projecting %s (offset uncommitted, will retry): %s",
                topic,
                err,
            )
            raise

        if projected:
            self._stats.events_projected += 1
            self._stats.last_projected_at = datetime.now(UTC)
            ts = self._stats.topic_stats.setdefault(
                topic, {"projected": 0, "errors": 0}
            )
            ts["projected"] += 1
            await self._update_watermark(f"{topic}:{msg.partition}", msg.offset)

        # Commit on a successful projection AND on a handled-but-not-applicable
        # message (project_event returned False for a topic this runner does not
        # project): both are fully accounted for, so the offset may advance. The
        # only path that does NOT reach here is a raised projection error above.
        await self._commit_message(msg)

    async def _route_poison_to_dlq(
        self,
        topic: str,
        data: dict[str, Any],
        err: BaseException,
        meta: MessageMeta,
    ) -> bool:
        """Publish a POISON event to the contract-declared poison DLQ topic.

        OMN-13634: the base-class safety net for a POISON ``project_event`` error
        that escaped a handler. Returns ``True`` when an envelope was published,
        ``False`` when the subclass declared no ``poison_dlq_topics`` (the caller
        still commits — the alternative is an unprocessable message wedging the
        partition forever; the loud ERROR log is the durable signal). Best-effort:
        a DLQ publish failure is logged, not raised.
        """
        dlq_topics = self.poison_dlq_topics
        if not dlq_topics:
            logger.error(
                "POISON event on %s has NO poison DLQ topic declared "
                "(committing to avoid a wedged partition): %s",
                topic,
                err,
            )
            return False
        correlation_id = correlation_id_from_payload(data, fallback=meta.fallback_id)
        envelope = build_dlq_envelope(
            original_message=data,
            failure_reason=f"poison projection error: {err}",
            handler=type(self).__name__,
            correlation_id=correlation_id,
        )
        value = json.dumps(envelope, default=str).encode("utf-8")
        try:
            await self.publish_dlq(dlq_topics[0], value)
        except Exception as publish_err:
            logger.error(
                "Failed to route POISON event on %s to DLQ %s (correlation_id=%s): %s",
                topic,
                dlq_topics[0],
                correlation_id,
                publish_err,
            )
            return False
        return True

    async def _commit_message(self, msg: Any) -> None:
        """Explicitly commit the offset for a fully-accounted-for message.

        OMN-13350: replaces the removed enable_auto_commit timer. A commit
        failure is logged but not raised — the offset stays where it is and the
        message is re-read, which is the safe direction (at-least-once), not a
        silent drop.
        """
        if self._consumer is None:
            return
        try:
            await self._consumer.commit(
                {
                    TopicPartition(msg.topic, msg.partition): msg.offset + 1,
                }
            )
        except Exception as err:
            logger.warning(
                "Failed to commit offset for %s:%d@%d: %s",
                msg.topic,
                msg.partition,
                msg.offset,
                err,
            )

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
