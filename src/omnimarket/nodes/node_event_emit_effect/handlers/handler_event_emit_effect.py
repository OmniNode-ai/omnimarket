# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Effect handler for ``node_event_emit_effect`` (OMN-15965 R1).

Thin-publish to Kafka with a file-based spool outbox as durability. The
handler constructor performs no I/O -- only ``handle()`` touches disk or
network (asserted by ``tests/unit/nodes/node_event_emit_effect/
test_contract_compliance.py``).

Envelope enrichment (OMN-16048 / OMN-16018 / OMN-16020). Before spooling,
``handle()`` reproduces the legacy daemon's publish-time enrichment exactly:
inject the standard metadata fields, apply the registry's per-fan-out-rule
payload transform, and derive the Kafka partition key from the registry's
declared ``partition_key_field`` against the POST-transform payload. See
``../enrichment.py`` for the field-by-field port and the determinism seam.
The enriched, per-topic payload is frozen into the spool record, so a
restart-replayed event goes out with the bytes it was enriched with, not
re-enriched under a later clock.

Drain semantics on each ``handle()`` invocation:
    1. Enrich, then append ONE spool record per fan-out topic (mirroring the
       daemon's one-queued-event-per-fan-out-rule shape).
    2. Attempt to publish each of the current event's records (skipped
       entirely in spool-only mode -- the explicit, contract-declared
       ``ONEX_EMIT_EFFECT_SPOOL_ONLY`` opt-out; see ``_build_default_adapter``
       for why an absent ``KAFKA_BOOTSTRAP_SERVERS`` alone no longer selects
       this mode, OMN-16167).
    3. On success, ack (delete that record's spool file).
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

from omnimarket.nodes.node_event_emit_effect.enrichment import (
    apply_transform,
    default_clock,
    default_correlation_id_factory,
    derive_partition_key,
    inject_metadata,
)
from omnimarket.nodes.node_event_emit_effect.errors import NonMappingPayloadError
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
    ResolvedTopic,
    UnknownEventTypeError,
    resolve_event_type,
    resolve_override_transform,
    resolve_partition_key_field,
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

# OMN-16167: the ONE sanctioned, contract-declared opt-out for a lane that is
# intentionally running with no Kafka publish target yet (local unit tests, a
# lane mid-bring-up). Declared in contract.yaml's env_dependencies block.
# This must never be conflated with "KAFKA_BOOTSTRAP_SERVERS happens to be
# unset" -- see _build_default_adapter().
_SPOOL_ONLY_ENV_VAR = "ONEX_EMIT_EFFECT_SPOOL_ONLY"
_SPOOL_ONLY_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


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

        # OMN-16049: do NOT seed ``environment`` from a bespoke, omniclaude-
        # namespaced variable. ``os.environ.get(..., "")`` returned "" on every
        # deployment that does not set it (onex-dev does not), and the config
        # model REJECTS empty ("environment cannot be empty") -- so this
        # override replaced the model's own valid ``default="local"`` with a
        # value guaranteed to raise. ``_try_publish``'s broad ``except
        # Exception`` then swallowed it into ``published=False``, making a
        # permanent total publish outage look like documented spool-only mode.
        # Leave the field unset: the model's default stands, and
        # ``apply_environment_overrides()`` applies the runtime's STANDARD
        # source (``KAFKA_ENVIRONMENT``) when it is set.
        config = ModelKafkaEventBusConfig(
            bootstrap_servers=self._bootstrap_servers,
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
        clock: Callable[[], datetime] = default_clock,
        correlation_id_factory: Callable[[], str] = default_correlation_id_factory,
    ) -> None:
        """No I/O here -- ``spool``/``publish_adapter`` are injected, not built.

        ``drain_budget_seconds`` is the TOTAL per-invocation publish budget --
        shared across the current event's own publish attempt and the
        backlog drain that follows it (OMN-15987 finding 2 fix; previously
        drain-only, letting the current-event publish overrun uncounted).

        ``clock`` / ``correlation_id_factory`` are the enrichment determinism
        seam (OMN-16048). Their defaults are exactly the daemon's inline
        expressions (``datetime.now(UTC)`` / ``str(uuid4())``), so runtime
        behavior is unchanged; the parity harness overrides both on BOTH
        paths so the two implementations' generated ``emitted_at`` /
        ``correlation_id`` are comparable instead of trivially unequal.
        """
        self._spool = spool
        self._publish_adapter = publish_adapter
        self._spool_dir = spool_dir
        self._drain_budget_seconds = drain_budget_seconds
        self._max_drain_count = max_drain_count
        self._clock = clock
        self._correlation_id_factory = correlation_id_factory

    def _resolve_targets(
        self, request: ModelEmitRequest
    ) -> tuple[tuple[ResolvedTopic, ...], str | None]:
        """Resolve the fan-out targets and the registry partition-key field."""
        if not request.topic:
            resolved_topics = resolve_event_type(request.event_type)
            return resolved_topics, resolve_partition_key_field(request.event_type)

        try:
            resolved_topics = resolve_event_type(request.event_type)
            tier = resolve_tier(resolved_topics)
            partition_key_field = resolve_partition_key_field(request.event_type)
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
            partition_key_field = None
        # An override topic has no fan-out rule of its own, but it still has
        # a declared redaction posture -- resolved from the topic's own
        # registry declaration, else from the event's (OMN-17237, H2). This
        # previously hardcoded transform_name=None, which made the override
        # field a bypass of the entire registry: an override onto
        # agent.chat.broadcast's observability topic published the raw `body`
        # that topic's declared strip_body exists to remove. A posture that
        # cannot be resolved raises rather than defaulting to verbatim.
        #
        # The registry's partition_key_field still applies when event_type IS
        # registered, so an override does not silently lose key-based ordering.
        return (
            ResolvedTopic(
                topic=request.topic,
                tier=tier,
                transform_name=resolve_override_transform(
                    request.topic, request.event_type
                ),
            ),
        ), partition_key_field

    def _build_messages(
        self,
        request: ModelEmitRequest,
        targets: tuple[ResolvedTopic, ...],
        partition_key_field: str | None,
    ) -> list[tuple[str, JsonType, str | None]]:
        """Enrich once, then transform + key per fan-out topic.

        Mirrors ``EmitSocketServer._handle_emit``: metadata is injected ONCE
        into the raw payload, then each fan-out rule's transform is applied to
        that shared enriched payload, and the partition key is derived from
        each rule's own post-transform result.

        A non-dict ``payload`` (the model permits list/str/number/bool/None)
        has no field surface for a transform to act on, so it is REFUSED
        rather than published (OMN-17237, H3). It previously returned here
        verbatim, un-enriched and un-transformed -- and a bare string is
        exactly the shape captured command output arrives in, i.e. the
        content a redaction posture exists to hold. The legacy daemon already
        rejects these (``socket_server._handle_emit``: "'payload' must be a
        JSON object"), so refusing narrows this node back to daemon parity
        rather than imposing a new restriction.

        ``None`` is not refused: the daemon maps it to ``{}`` before its own
        object check, and an empty object has nothing to disclose. Mirroring
        that keeps the null case at parity instead of trading a disclosure
        hole for a parity regression.

        Raises:
            NonMappingPayloadError: ``payload`` is neither a JSON object nor
                null.
        """
        raw = request.payload
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise NonMappingPayloadError(
                event_type=request.event_type,
                payload_type=type(raw).__name__,
            )

        enriched = inject_metadata(
            raw,
            request.correlation_id,
            clock=self._clock,
            correlation_id_factory=self._correlation_id_factory,
        )
        messages: list[tuple[str, JsonType, str | None]] = []
        for target in targets:
            transformed = apply_transform(target.transform_name, enriched)
            key = (
                request.partition_key
                if request.partition_key is not None
                else derive_partition_key(partition_key_field, transformed)
            )
            messages.append((target.topic, transformed, key))
        return messages

    @staticmethod
    def _envelope_correlation_id(payload: JsonType, fallback: str | None) -> str | None:
        """Correlation ID carried on the message envelope for this payload.

        The daemon's publisher loop reads it back out of the payload
        (``publisher_loop._publish_event``), so post-enrichment the payload is
        the authority -- not the request field, which is only a seed.
        """
        if isinstance(payload, dict):
            value = payload.get("correlation_id")
            if isinstance(value, str):
                return value
        return fallback

    def handle(self, request: ModelEmitRequest) -> ModelEmitResult:
        spool = self._spool if self._spool is not None else self._build_default_spool()

        targets, partition_key_field = self._resolve_targets(request)
        tier = resolve_tier(targets)
        messages = self._build_messages(request, targets, partition_key_field)
        queued_at = self._clock()

        appended: list[SpoolFile] = []
        dropped_count = 0
        for topic, payload, partition_key in messages:
            outcome = spool.append(
                SpoolRecord(
                    event_id=request.event_id,
                    event_type=request.event_type,
                    topic=topic,
                    tier=tier,
                    payload=payload,
                    partition_key=partition_key,
                    correlation_id=self._envelope_correlation_id(
                        payload, request.correlation_id
                    ),
                    queued_at=queued_at,
                )
            )
            dropped_count += outcome.dropped_count
            if outcome.spool_file is not None:
                appended.append(outcome.spool_file)

        result_correlation_id = self._envelope_correlation_id(
            messages[0][1] if messages else None, request.correlation_id
        )

        # Adapter resolution happens AFTER the append loop above (OMN-16167):
        # a loud failure out of _build_default_adapter() (genuinely
        # unconfigured Kafka target, no spool_only opt-out declared) must not
        # cost the current event its durability -- it is already on disk by
        # this point, ready for the next invocation's drain once Kafka is
        # configured.
        adapter = (
            self._publish_adapter
            if self._publish_adapter is not None
            else self._build_default_adapter()
        )

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
                dropped_count=dropped_count,
                correlation_id=result_correlation_id,
            )

        # Records rejected at append time (oversized telemetry -- see
        # SpoolOutbox._append_telemetry) were never spooled, so they cannot be
        # published; the event only counts as published when every one of its
        # fan-out topics went out.
        published_topics: list[str] = []
        publish_failed = False
        for spool_file in appended:
            if self._try_publish(adapter, spool_file, spool, deadline=deadline):
                published_topics.append(spool_file.record.topic)
            else:
                publish_failed = True
                break
        all_published = (
            not publish_failed and len(appended) == len(messages) and bool(messages)
        )

        # The backlog drain is gated on a real publish FAILURE only -- an
        # append-time drop of the current event says nothing about whether
        # the broker is reachable, so the pre-existing backlog still gets its
        # opportunistic drain.
        drained_count = 0
        if not publish_failed:
            drained_count = self._drain_backlog(
                adapter,
                spool,
                exclude={f.path for f in appended},
                deadline=deadline,
            )

        return ModelEmitResult(
            event_id=request.event_id,
            topics_published=published_topics if all_published else [],
            published=all_published,
            spool_only=False,
            drained_count=drained_count,
            dropped_count=dropped_count,
            correlation_id=result_correlation_id,
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
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                # Budget already exhausted before this topic even got a
                # chance -- fail fast rather than attempt a publish with
                # a zero/negative timeout (OMN-15987 finding 2).
                raise TimeoutError(
                    f"Publish budget exhausted before topic {record.topic!r} "
                    f"for event {record.event_id}"
                )
            adapter.publish(
                record.topic,
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
        exclude: set[Path],
        deadline: float,
    ) -> int:
        drained = 0
        for spool_file in spool.list_pending():
            if spool_file.path in exclude:
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
        """Resolve the Kafka publish target via the shared config model (OMN-16167).

        This no longer reads ``KAFKA_BOOTSTRAP_SERVERS`` directly to decide
        behavior -- the operator ruled that pattern out org-wide (OMN-15938):
        a bare env-var read silently choosing "no publish target" hides a
        real misconfiguration behind an indistinguishable-looking spool-only
        mode.

        Spool-only mode is now an explicit, contract-declared opt-out
        (``ONEX_EMIT_EFFECT_SPOOL_ONLY`` -- see contract.yaml
        ``env_dependencies``), for a lane that is genuinely, intentionally
        running with no Kafka target yet (local unit tests, a lane
        mid-bring-up). Absent that opt-out, ``bootstrap_servers`` is
        resolved through ``ModelKafkaEventBusConfig`` -- the same shared,
        env-override-aware config model every other Kafka publisher in this
        codebase already builds its connection from (this class's own
        ``_build_default_bus``, ``node_emit_daemon.kafka_publish``). That
        model's ``bootstrap_servers`` field has no default: construction
        fails loudly (``KeyError``) if the var is genuinely missing, instead
        of this handler silently choosing spool-only on the operator's
        behalf. ``handle()`` resolves the adapter only after the current
        event has already been appended to the spool outbox, so a loud
        failure here never loses the event -- it stays durable on disk for
        the next invocation once Kafka is configured.
        """
        if self._spool_only_opt_out():
            return None

        from omnibase_infra.event_bus.models.config import ModelKafkaEventBusConfig

        config = ModelKafkaEventBusConfig().apply_environment_overrides()
        return KafkaEventPublisher(config.bootstrap_servers)

    @staticmethod
    def _spool_only_opt_out() -> bool:
        raw = os.environ.get(_SPOOL_ONLY_ENV_VAR, "")
        return raw.strip().lower() in _SPOOL_ONLY_TRUE_VALUES


__all__: list[str] = [
    "HandlerEventEmitEffect",
    "KafkaEventPublisher",
    "ProtocolPublishAdapter",
]
