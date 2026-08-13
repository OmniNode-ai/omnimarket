# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Effect handler for ``node_event_emit_effect`` (OMN-15965 R1).

Thin-publish to Kafka with a file-based spool outbox as durability. The
handler constructor performs no I/O -- only ``handle()`` touches disk or
network (asserted by ``tests/unit/nodes/node_event_emit_effect/
test_contract_compliance.py``).

Drain semantics on each ``handle()`` invocation:
    1. Append the current request to the spool.
    2. Attempt to publish the current event (skipped entirely in
       spool-only mode, i.e. ``KAFKA_BOOTSTRAP_SERVERS`` unset).
    3. On success, ack (delete its spool file).
    4. Drain remaining spool files oldest-first up to a bounded
       per-invocation budget (``timeout_ms`` on the contract bounds this).
    5. Stop on first publish failure mid-drain, leaving the remainder
       un-acked on disk for the next invocation -- no data loss, no
       double-ack.
    6. Return counts.

This node must never import ``omnimarket.nodes.node_emit_daemon.*`` -- see
``spool/topic_resolver.py`` for why. The Kafka publish path below is written
locally rather than reused from ``node_emit_daemon.kafka_publish`` for the
same reason.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid4

from omnimarket.nodes.node_event_emit_effect.models.model_emit_request import (
    JsonType,
    ModelEmitRequest,
)
from omnimarket.nodes.node_event_emit_effect.models.model_emit_result import (
    ModelEmitResult,
)
from omnimarket.nodes.node_event_emit_effect.spool.spool_outbox import (
    SpoolFile,
    SpoolOutbox,
    SpoolRecord,
)
from omnimarket.nodes.node_event_emit_effect.spool.topic_resolver import (
    resolve_event_type,
    resolve_tier,
)

logger = logging.getLogger(__name__)

_DEFAULT_DRAIN_BUDGET_SECONDS = 8.0  # margin under contract.descriptor.timeout_ms=10000
_DEFAULT_MAX_DRAIN_COUNT = 500
_DEFAULT_PUBLISH_TIMEOUT_SECONDS = 5.0  # bounds a single publish so one hung
# broker connection can't overrun _drain_budget_seconds -- _drain_backlog only
# checks its deadline between records, not during a single publish call.


class ProtocolPublishAdapter(Protocol):
    """Effect boundary for publishing one payload to one topic."""

    def publish(
        self,
        topic: str,
        payload: JsonType,
        *,
        key: str | None,
        correlation_id: str | None,
    ) -> None:
        """Publish one message. Raise on failure."""
        ...


class KafkaEventPublisher:
    """Synchronous Kafka publish adapter built from ``KAFKA_BOOTSTRAP_SERVERS``.

    Constructed lazily inside ``HandlerEventEmitEffect.handle()`` -- never in
    ``__init__`` -- so the handler constructor stays I/O-free. Each call
    opens a fresh event-bus connection and closes it after use:
    ``node_event_emit_effect`` is invoked opportunistically (CLI /
    plugin-runtime dispatch), not as a resident daemon, so per-call
    connect/close is the correct default for R1. The contract's descriptor
    notes the runtime can also host this node for resident draining later.
    """

    def __init__(
        self,
        bootstrap_servers: str,
        *,
        source: str = "node_event_emit_effect",
        publish_timeout_seconds: float = _DEFAULT_PUBLISH_TIMEOUT_SECONDS,
    ) -> None:
        self._bootstrap_servers = bootstrap_servers
        self._source = source
        self._publish_timeout_seconds = publish_timeout_seconds

    def publish(
        self,
        topic: str,
        payload: JsonType,
        *,
        key: str | None,
        correlation_id: str | None,
    ) -> None:
        async def _run() -> None:
            await asyncio.wait_for(
                self._publish_async(
                    topic, payload, key=key, correlation_id=correlation_id
                ),
                timeout=self._publish_timeout_seconds,
            )

        asyncio.run(_run())

    async def _publish_async(
        self,
        topic: str,
        payload: JsonType,
        *,
        key: str | None,
        correlation_id: str | None,
    ) -> None:
        from omnibase_infra.event_bus.event_bus_kafka import EventBusKafka
        from omnibase_infra.event_bus.models import ModelEventHeaders
        from omnibase_infra.event_bus.models.config import ModelKafkaEventBusConfig

        config = ModelKafkaEventBusConfig(
            bootstrap_servers=self._bootstrap_servers,
            environment=os.environ.get("OMNICLAUDE_PUBLISHER_ENVIRONMENT", ""),
        ).apply_environment_overrides()
        bus = EventBusKafka(config)
        await bus.start()
        try:
            try:
                resolved_correlation_id = (
                    UUID(correlation_id) if correlation_id else uuid4()
                )
            except ValueError:
                resolved_correlation_id = uuid4()
            headers = ModelEventHeaders(
                source=self._source,
                event_type=topic,
                timestamp=datetime.now(UTC),
                correlation_id=resolved_correlation_id,
            )
            await bus.publish(
                topic=topic,
                key=key.encode("utf-8") if key else None,
                value=json.dumps(payload).encode("utf-8"),
                headers=headers,
            )
        finally:
            await bus.stop()


def _default_spool_dir() -> Path:
    """Default spool directory: env override, else XDG_RUNTIME_DIR, else /tmp.

    Deliberately a *new* directory name (``event-emit-effect-spool``),
    distinct from ``node_emit_daemon``'s existing ``event-spool`` /
    ``event-outbox`` names -- the daemon is not deleted until R5, so during
    the coexistence window the two must not collide on disk.
    """
    override = os.environ.get("ONEX_EMIT_EFFECT_SPOOL_DIR")
    if override:
        return Path(override)
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return Path(xdg) / "onex" / "event-emit-effect-spool"
    return (
        Path("/tmp") / "onex-event-emit-effect-spool"
    )  # local-path-ok: documented fallback


class HandlerEventEmitEffect:
    """Effect handler: resolve topic(s)+tier, spool, publish, opportunistically drain."""

    def __init__(
        self,
        *,
        spool: SpoolOutbox | None = None,
        publish_adapter: ProtocolPublishAdapter | None = None,
        spool_dir: Path | None = None,
        drain_budget_seconds: float = _DEFAULT_DRAIN_BUDGET_SECONDS,
        max_drain_count: int = _DEFAULT_MAX_DRAIN_COUNT,
    ) -> None:
        """No I/O here -- ``spool``/``publish_adapter`` are injected, not built."""
        self._spool = spool
        self._publish_adapter = publish_adapter
        self._spool_dir = spool_dir
        self._drain_budget_seconds = drain_budget_seconds
        self._max_drain_count = max_drain_count

    def handle(self, request: ModelEmitRequest) -> ModelEmitResult:
        spool = self._spool if self._spool is not None else self._build_default_spool()
        adapter = (
            self._publish_adapter
            if self._publish_adapter is not None
            else self._build_default_adapter()
        )

        resolved_topics = resolve_event_type(request.event_type)
        tier = resolve_tier(resolved_topics)
        topics: tuple[str, ...] = (
            (request.topic,)
            if request.topic
            else tuple(t.topic for t in resolved_topics)
        )

        record = SpoolRecord(
            event_id=request.event_id,
            event_type=request.event_type,
            topics=topics,
            tier=tier,
            payload=request.payload,
            partition_key=request.partition_key,
            correlation_id=request.correlation_id,
            queued_at=datetime.now(UTC),
        )
        outcome = spool.append(record)

        if adapter is None:
            return ModelEmitResult(
                event_id=request.event_id,
                topics_published=[],
                published=False,
                drained_count=0,
                dropped_count=outcome.dropped_count,
                correlation_id=request.correlation_id,
            )

        if outcome.spool_file is None:
            # The current record itself was rejected (oversized telemetry --
            # see SpoolOutbox._append_telemetry); nothing was spooled for it,
            # so there is nothing to publish. Still opportunistically drain
            # any existing backlog -- that work is independent of whether the
            # current event could be spooled.
            drained_count = self._drain_backlog(adapter, spool, exclude=None)
            return ModelEmitResult(
                event_id=request.event_id,
                topics_published=[],
                published=False,
                drained_count=drained_count,
                dropped_count=outcome.dropped_count,
                correlation_id=request.correlation_id,
            )

        published = self._try_publish(adapter, outcome.spool_file, spool)
        drained_count = 0
        if published:
            drained_count = self._drain_backlog(
                adapter, spool, exclude=outcome.spool_file.path
            )

        return ModelEmitResult(
            event_id=request.event_id,
            topics_published=list(topics) if published else [],
            published=published,
            drained_count=drained_count,
            dropped_count=outcome.dropped_count,
            correlation_id=request.correlation_id,
        )

    def _try_publish(
        self,
        adapter: ProtocolPublishAdapter,
        spool_file: SpoolFile,
        spool: SpoolOutbox,
    ) -> bool:
        record = spool_file.record
        try:
            for topic in record.topics:
                adapter.publish(
                    topic,
                    record.payload,
                    key=record.partition_key,
                    correlation_id=record.correlation_id,
                )
        except Exception:
            logger.warning(
                "Publish failed for event %s; leaving spooled for retry",
                record.event_id,
                exc_info=True,
            )
            return False
        spool.ack(spool_file.path)
        return True

    def _drain_backlog(
        self,
        adapter: ProtocolPublishAdapter,
        spool: SpoolOutbox,
        *,
        exclude: Path | None,
    ) -> int:
        deadline = time.monotonic() + self._drain_budget_seconds
        drained = 0
        for spool_file in spool.list_pending():
            if exclude is not None and spool_file.path == exclude:
                continue
            if drained >= self._max_drain_count or time.monotonic() >= deadline:
                break
            if not self._try_publish(adapter, spool_file, spool):
                break
            drained += 1
        return drained

    def _build_default_spool(self) -> SpoolOutbox:
        spool_dir = (
            self._spool_dir if self._spool_dir is not None else _default_spool_dir()
        )
        return SpoolOutbox(spool_dir)

    def _build_default_adapter(self) -> ProtocolPublishAdapter | None:
        bootstrap = os.environ.get("KAFKA_BOOTSTRAP_SERVERS")
        if not bootstrap:
            return None
        return KafkaEventPublisher(bootstrap)


__all__: list[str] = [
    "HandlerEventEmitEffect",
    "KafkaEventPublisher",
    "ProtocolPublishAdapter",
]
