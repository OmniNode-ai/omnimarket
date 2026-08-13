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
import threading
import time
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
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
    EnumDurabilityTier,
    UnknownEventTypeError,
    resolve_event_type,
    resolve_tier,
)

logger = logging.getLogger(__name__)

# Total per-invocation publish budget, shared across the current event's own
# publish AND the opportunistic backlog drain that follows it -- margin
# under contract.descriptor.timeout_ms=10000 (OMN-15987 finding 2: the prior
# split of "5s current + 8s drain, deadline checked only between records"
# could overrun the declared timeout by ~2x because the current event's
# publish wasn't counted against any budget and the deadline was never
# checked mid-topic-loop). A single shared deadline, checked before every
# topic publish (not just between spool records), bounds the true wall-clock
# total to this constant plus negligible scheduling overhead.
_DEFAULT_TOTAL_BUDGET_SECONDS = 9.0
_DEFAULT_MAX_DRAIN_COUNT = 500
_DEFAULT_PUBLISH_TIMEOUT_SECONDS = 5.0  # per-adapter fallback when no explicit
# per-call timeout_seconds is supplied (only relevant to callers that build a
# KafkaEventPublisher directly, outside HandlerEventEmitEffect).
_SINGLE_PUBLISH_CAP_SECONDS = 5.0  # a single publish is never granted more
# than this from the remaining shared budget, so one hung topic in a
# multi-topic fan-out cannot consume the whole remaining budget itself.
_LOOP_HANDOFF_MARGIN_SECONDS = 1.0  # extra slack given to the background-loop
# future.result() wait beyond the coroutine's own internal timeout, to
# absorb thread-handoff scheduling jitter without masking a real timeout.


class ProtocolPublishAdapter(Protocol):
    """Effect boundary for publishing one payload to one topic."""

    def publish(
        self,
        topic: str,
        payload: JsonType,
        *,
        key: str | None,
        correlation_id: str | None,
        timeout_seconds: float | None = None,
    ) -> None:
        """Publish one message. Raise on failure.

        ``timeout_seconds``, when given, overrides the adapter's own default
        for this call only -- callers computing a shared per-invocation
        publish budget (see ``HandlerEventEmitEffect._try_publish``) pass the
        remaining budget here so no single publish can overrun it.
        """
        ...


class _KafkaPublishLoop:
    """Process-wide background thread running one dedicated asyncio loop.

    ``KafkaEventPublisher.publish()`` is a *synchronous* method that must
    work correctly whether the calling thread already has its own running
    event loop or not:

    * Canonical dispatch (``RuntimeDispatch._invoke`` in omnibase_core,
      ``NodeEffect._execute``) is ``async def`` and calls a sync-def-B
      handler's ``handle()`` directly from inside a running loop -- so
      ``publish()`` executes on a thread whose loop is already running.
    * Direct CLI / script invocation has no running loop at all.

    ``asyncio.run()`` only handles the second case; it raises
    ``RuntimeError: asyncio.run() cannot be called from a running event
    loop`` in the first (OMN-15987 finding 1 -- silently swallowed by
    ``_try_publish``'s broad ``except Exception``, indistinguishable from
    documented spool-only mode).

    Submitting the publish coroutine to a loop that lives on its own
    dedicated thread sidesteps the question entirely: the coroutine never
    runs on the calling thread, so the calling thread's loop state is
    irrelevant. This is a lazily-started, shared, daemon-threaded loop --
    not spun up per call -- so steady-state publish cost stays low.
    """

    _instance_lock = threading.Lock()
    _instance: _KafkaPublishLoop | None = None

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_forever,
            name="node-event-emit-effect-kafka-loop",
            daemon=True,
        )
        self._thread.start()

    def _run_forever(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    @classmethod
    def shared(cls) -> _KafkaPublishLoop:
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def run(self, coro: Coroutine[Any, Any, None], *, timeout: float) -> None:
        """Run ``coro`` on the background loop; block the caller for the result.

        ``timeout`` bounds this call from the *calling* thread's side (belt
        and suspenders on top of the coroutine's own internal
        ``asyncio.wait_for``) -- if the background loop is wedged, this
        raises ``concurrent.futures.TimeoutError`` rather than hanging
        forever. That exception is an ``Exception`` subclass, so it is
        caught by ``HandlerEventEmitEffect._try_publish``'s existing
        failure handling like any other publish failure.
        """
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        future.result(timeout=timeout)


class KafkaEventPublisher:
    """Synchronous Kafka publish adapter built from ``KAFKA_BOOTSTRAP_SERVERS``.

    Constructed lazily inside ``HandlerEventEmitEffect.handle()`` -- never in
    ``__init__`` -- so the handler constructor stays I/O-free. Each call
    opens a fresh event-bus connection and closes it after use:
    ``node_event_emit_effect`` is invoked opportunistically (CLI /
    plugin-runtime dispatch), not as a resident daemon, so per-call
    connect/close is the correct default for R1. The contract's descriptor
    notes the runtime can also host this node for resident draining later.

    ``publish()`` itself is synchronous by contract (``ProtocolPublishAdapter``)
    but never blocks the calling thread's own event loop -- see
    ``_KafkaPublishLoop`` for why.
    """

    def __init__(
        self,
        bootstrap_servers: str,
        *,
        source: str = "node_event_emit_effect",
        publish_timeout_seconds: float = _DEFAULT_PUBLISH_TIMEOUT_SECONDS,
        bus_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._bootstrap_servers = bootstrap_servers
        self._source = source
        self._publish_timeout_seconds = publish_timeout_seconds
        # Test-injection seam: a fake bus (e.g. EventBusInmemory) can stand
        # in for the real Kafka connection so KafkaEventPublisher's actual
        # publish/timeout/loop-handoff logic gets real test coverage without
        # a live broker. Defaults to building the real EventBusKafka.
        self._bus_factory: Callable[[], Any] = (
            bus_factory if bus_factory is not None else self._build_default_bus
        )

    def _build_default_bus(self) -> Any:
        from omnibase_infra.event_bus.event_bus_kafka import EventBusKafka
        from omnibase_infra.event_bus.models.config import ModelKafkaEventBusConfig

        config = ModelKafkaEventBusConfig(
            bootstrap_servers=self._bootstrap_servers,
            environment=os.environ.get("OMNICLAUDE_PUBLISHER_ENVIRONMENT", ""),
        ).apply_environment_overrides()
        return EventBusKafka(config)

    def publish(
        self,
        topic: str,
        payload: JsonType,
        *,
        key: str | None,
        correlation_id: str | None,
        timeout_seconds: float | None = None,
    ) -> None:
        effective_timeout = (
            timeout_seconds
            if timeout_seconds is not None
            else self._publish_timeout_seconds
        )
        _KafkaPublishLoop.shared().run(
            self._publish_async(
                topic,
                payload,
                key=key,
                correlation_id=correlation_id,
                timeout_seconds=effective_timeout,
            ),
            timeout=effective_timeout + _LOOP_HANDOFF_MARGIN_SECONDS,
        )

    async def _publish_async(
        self,
        topic: str,
        payload: JsonType,
        *,
        key: str | None,
        correlation_id: str | None,
        timeout_seconds: float,
    ) -> None:
        await asyncio.wait_for(
            self._publish_once(topic, payload, key=key, correlation_id=correlation_id),
            timeout=timeout_seconds,
        )

    async def _publish_once(
        self,
        topic: str,
        payload: JsonType,
        *,
        key: str | None,
        correlation_id: str | None,
    ) -> None:
        from omnibase_infra.event_bus.models import ModelEventHeaders

        bus = self._bus_factory()
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
            # NOTE (OMN-15987): the prior code called `bus.stop()`, which does
            # not exist on EventBusKafka (its lifecycle methods are start /
            # initialize / shutdown / close) -- every real publish would have
            # raised AttributeError out of this finally block. Never caught
            # by CI because KafkaEventPublisher had zero test coverage.
            await bus.close()


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
        drain_budget_seconds: float = _DEFAULT_TOTAL_BUDGET_SECONDS,
        max_drain_count: int = _DEFAULT_MAX_DRAIN_COUNT,
    ) -> None:
        """No I/O here -- ``spool``/``publish_adapter`` are injected, not built.

        ``drain_budget_seconds`` is the TOTAL per-invocation publish budget --
        shared across the current event's own publish attempt and the
        backlog drain that follows it (OMN-15987 finding 2 fix; previously
        drain-only, letting the current-event publish overrun uncounted).
        """
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

        if request.topic:
            topics: tuple[str, ...] = (request.topic,)
            try:
                resolved_topics = resolve_event_type(request.event_type)
                tier = resolve_tier(resolved_topics)
            except UnknownEventTypeError:
                # Deliberate escape hatch (OMN-15987 finding 4): a topic
                # override may target an event_type that is legitimately
                # outside the registry's scope entirely -- resolve_event_type
                # must not veto that. Default to the conservative tier
                # (telemetry: bounded, drop-oldest) so an unregistered
                # override never inherits never-drop duty_critical semantics
                # it was never declared for. When event_type IS registered,
                # its registry-derived tier still applies even with an
                # override topic (secondary note in the finding) -- the
                # override changes where it publishes, not how durable it is.
                tier = EnumDurabilityTier.TELEMETRY
        else:
            resolved_topics = resolve_event_type(request.event_type)
            tier = resolve_tier(resolved_topics)
            topics = tuple(t.topic for t in resolved_topics)

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

        # Single shared deadline for this invocation: current-event publish
        # + backlog drain both draw from the same budget (OMN-15987 finding 2).
        deadline = time.monotonic() + self._drain_budget_seconds

        if adapter is None:
            return ModelEmitResult(
                event_id=request.event_id,
                topics_published=[],
                published=False,
                spool_only=True,
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
            drained_count = self._drain_backlog(
                adapter, spool, exclude=None, deadline=deadline
            )
            return ModelEmitResult(
                event_id=request.event_id,
                topics_published=[],
                published=False,
                spool_only=False,
                drained_count=drained_count,
                dropped_count=outcome.dropped_count,
                correlation_id=request.correlation_id,
            )

        published = self._try_publish(
            adapter, outcome.spool_file, spool, deadline=deadline
        )
        drained_count = 0
        if published:
            drained_count = self._drain_backlog(
                adapter, spool, exclude=outcome.spool_file.path, deadline=deadline
            )

        return ModelEmitResult(
            event_id=request.event_id,
            topics_published=list(topics) if published else [],
            published=published,
            spool_only=False,
            drained_count=drained_count,
            dropped_count=outcome.dropped_count,
            correlation_id=request.correlation_id,
        )

    def _try_publish(
        self,
        adapter: ProtocolPublishAdapter,
        spool_file: SpoolFile,
        spool: SpoolOutbox,
        *,
        deadline: float,
    ) -> bool:
        record = spool_file.record
        try:
            for topic in record.topics:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    # Budget already exhausted before this topic even got a
                    # chance -- fail fast rather than attempt a publish with
                    # a zero/negative timeout (OMN-15987 finding 2).
                    raise TimeoutError(
                        f"Publish budget exhausted before topic {topic!r} "
                        f"for event {record.event_id}"
                    )
                adapter.publish(
                    topic,
                    record.payload,
                    key=record.partition_key,
                    correlation_id=record.correlation_id,
                    timeout_seconds=min(remaining, _SINGLE_PUBLISH_CAP_SECONDS),
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
        deadline: float,
    ) -> int:
        drained = 0
        for spool_file in spool.list_pending():
            if exclude is not None and spool_file.path == exclude:
                continue
            if drained >= self._max_drain_count or time.monotonic() >= deadline:
                break
            if not self._try_publish(adapter, spool_file, spool, deadline=deadline):
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
