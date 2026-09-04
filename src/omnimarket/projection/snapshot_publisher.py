# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Snapshot-delta encoding, and a publish seam a SYNC projection handler can use.

OMN-17774 (GOAL row 0 leg (b)/(c), epic OMN-16776).

WHY THIS MODULE EXISTS
----------------------
OMN-15800 put ``publish_snapshot_delta`` on :class:`BaseProjectionRunner`. That
made the bus-fed serving path reachable from exactly one handler shape: a
projection whose writer subclasses the runner and therefore owns an asyncpg pool
and an ``AIOKafkaProducer``. ``ConsumerFlowProjectionWriter`` is that shape.

Most projection reducers are NOT. ``HandlerProjectionSessionReplay`` (and the 25
other in-process reducers) are plain sync handlers: the runtime auto-wiring
builds the database adapter, injects it as ``_db`` and calls
``handle(input_data)`` on a worker thread (``handler_wiring._invoke_projection``,
``asyncio.to_thread``). They hold no pool, no producer and no event loop. There
was no way for one of them to become ``bus_backed`` short of rewriting it into a
runner -- which for session-replay would mean discarding the runtime's
tenant-authority adapter construction and rewriting the OMN-17183 reducer-state
fix onto asyncpg. That is a rewrite, not a conversion, and it is why 55 of 59
exposures are still refusing.

So the encoding moves here and becomes the single authority, and a sync publish
seam is added beside it. ``BaseProjectionRunner.publish_snapshot_delta`` now
calls :func:`encode_snapshot_delta` rather than carrying a second copy: the key
bytes, the ``|``-delimiter refusal, the header set and the wire shape are
defined once. Two encoders that drift produce a tombstone whose key does not
match its own upsert -- a delete that silently misses its row.

WHAT THIS MODULE IS NOT
-----------------------
Not a daemon, not a poller, not a metrics exporter, not a ``Plugin*`` class.
:class:`KafkaSnapshotDeltaPublisher` owns nothing across calls: it opens a
producer, sends one message and closes it, inside the single event loop
``asyncio.run`` opens for that call. That is the same lifetime rule
``ConsumerFlowProjectionWriter`` documents for its own in-process path -- an
``AIOKafkaProducer`` is bound to the loop that created it, so anything cached
across ``asyncio.run`` boundaries belongs to a loop that no longer exists and
raises ``RuntimeError: Event loop is closed`` on first reuse (34 of those on the
.201 dev lane, zero rows written, DLQ climbing).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict

from omnimarket.config.settings import Settings
from omnimarket.projection.models import (
    ModelProjectionSnapshotDelta,
    ProjectionTableConfig,
    snapshot_json_value,
)

logger = logging.getLogger(__name__)

__all__ = [
    "KafkaSnapshotDeltaPublisher",
    "ModelSnapshotDeltaMessage",
    "ProtocolSnapshotDeltaPublisher",
    "encode_snapshot_delta",
    "resolve_snapshot_bootstrap_servers",
]

# The key-part delimiter. SnapshotCache splits a tombstone's raw key on this
# same character, so it is shared here rather than spelled twice.
_KEY_DELIMITER = "|"

_HOUSE_TENANT = "omninode"


class ModelSnapshotDeltaMessage(BaseModel):
    """One encoded snapshot-delta Kafka message, ready to send.

    ``value is None`` is a genuine Kafka tombstone (a ``delete``), not a JSON
    body carrying ``op="delete"`` -- only a null value lets compaction reclaim
    the key.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    topic: str
    key: bytes
    value: bytes | None
    headers: tuple[tuple[str, bytes], ...]


class ProtocolSnapshotDeltaPublisher(Protocol):
    """Transport for one encoded snapshot delta.

    Returns True when the message reached the broker. Returns False -- never
    raises -- when no transport is configured: a projection whose row is already
    durable in Postgres must not DLQ its source event because the republish leg
    is unavailable. The caller reports the False rather than swallowing it.
    """

    def publish(self, message: ModelSnapshotDeltaMessage) -> bool:
        """Send one encoded delta. True iff the broker accepted it."""
        ...


def resolve_snapshot_bootstrap_servers() -> str:
    """Broker list for the snapshot republish leg, or ``""`` when unconfigured.

    Reads the same ``Settings`` accessor ``BaseProjectionRunner`` resolves its
    own brokers through, so a runtime process and a standalone writer in the
    same lane cannot disagree about which broker the snapshot topics live on.
    """
    return Settings().get_effective_kafka_bootstrap_servers()


def encode_snapshot_delta(
    exposure: ProjectionTableConfig,
    *,
    op: Literal["upsert", "delete"],
    row: dict[str, Any] | None,
    source_event_id: str,
    source_topic: str,
    source_partition: int,
    source_offset: int,
    observed_at: str,
    tenant_id: str = _HOUSE_TENANT,
    key: dict[str, Any] | None = None,
) -> ModelSnapshotDeltaMessage | None:
    """Encode one keyed row-delta for a ``bus_backed`` exposure.

    Returns ``None`` when the exposure is not ``bus_backed`` -- whether anything
    is published at all is entirely contract-driven, never a per-call decision
    in a handler, so every call site calls this unconditionally after a
    successful write.

    An ``upsert`` derives its message key from ``row``; a ``delete`` has no row
    and derives its key from ``key`` instead (OMN-16150). ``key`` is required
    for a delete and forbidden for an upsert -- ``row`` is the single key source
    there, so an explicit ``key`` alongside it would be a second, possibly
    divergent source of truth.

    ``source_topic``/``source_partition``/``source_offset`` are the SOURCE
    event's own coordinates and are the ordering authority
    ``SnapshotCache.apply_message`` keys its staleness comparison on. They are
    required unconditionally (including for a ``delete``, where the tombstone
    wire shape does not carry them) so no caller can silently omit the ordering
    token. ``observed_at`` is display-only metadata and is never consulted for
    staleness; it is a parameter rather than a ``datetime.now()`` call so the
    encoding stays pure and byte-reproducible in a test.

    Raises ``RuntimeError`` -- never silently drops -- when the exposure is
    misconfigured (``bus_backed`` with no ``key_columns``), the key source is
    missing a declared key column, or ``row``/``key`` is supplied for the wrong
    ``op``. All are programming errors in the calling handler, never a runtime
    or network condition.
    """
    if not exposure.bus_backed:
        return None
    if not exposure.key_columns:
        raise RuntimeError(
            f"projection_api exposure {exposure.topic!r} is bus_backed "
            "but declares no key_columns"
        )

    key_source: dict[str, Any]
    if op == "upsert":
        if row is None:
            raise RuntimeError(
                f"encode_snapshot_delta({exposure.topic!r}, op='upsert') requires a row"
            )
        if key is not None:
            raise RuntimeError(
                f"encode_snapshot_delta({exposure.topic!r}, op='upsert') "
                "must not carry an explicit key (row is the key source)"
            )
        key_source = row
    else:
        if row is not None:
            raise RuntimeError(
                f"encode_snapshot_delta({exposure.topic!r}, op='delete') "
                "must not carry a row"
            )
        if key is None:
            raise RuntimeError(
                f"encode_snapshot_delta({exposure.topic!r}, op='delete') requires a key"
            )
        key_source = key

    try:
        key_parts = tuple(str(key_source[column]) for column in exposure.key_columns)
    except KeyError as exc:
        raise RuntimeError(
            f"snapshot delta for {exposure.topic!r} is missing declared "
            f"key column {exc}"
        ) from exc

    # Fail loud rather than silently corrupt the compacted topic: SnapshotCache
    # splits a tombstone's raw key on this delimiter, so an unescaped delimiter
    # inside a key-column value would make the recovered tombstone key tuple
    # diverge from the upsert's (a delete silently misses its row) and could
    # collide two distinct key tuples onto the same encoded bytes.
    if any(_KEY_DELIMITER in part for part in key_parts):
        raise RuntimeError(
            f"snapshot delta key for {exposure.topic!r} contains the "
            f"{_KEY_DELIMITER!r} delimiter and cannot be safely encoded: "
            f"{key_parts!r}"
        )

    headers: tuple[tuple[str, bytes], ...] = (
        ("tenant_id", tenant_id.encode("utf-8")),
        ("content_type", b"application/json"),
        ("schema_version", b"projection_snapshot.v1"),
    )
    key_bytes = _KEY_DELIMITER.join(key_parts).encode("utf-8")

    if op == "delete":
        return ModelSnapshotDeltaMessage(
            topic=exposure.topic, key=key_bytes, value=None, headers=headers
        )

    serialized_row = {
        column: snapshot_json_value(
            value, decode_json_string=column in exposure.json_columns
        )
        for column, value in (row or {}).items()
    }
    delta = ModelProjectionSnapshotDelta(
        topic=exposure.topic,
        key=key_parts,
        op="upsert",
        row=serialized_row,
        observed_at=observed_at,
        source_event_id=source_event_id,
        source_topic=source_topic,
        source_partition=source_partition,
        source_offset=source_offset,
    )
    return ModelSnapshotDeltaMessage(
        topic=exposure.topic,
        key=key_bytes,
        value=delta.model_dump_json().encode("utf-8"),
        headers=headers,
    )


class KafkaSnapshotDeltaPublisher:
    """Publish one encoded delta over a producer that lives for that one call.

    Used by the SYNC projection-handler path. The runtime dispatches those
    handlers on a worker thread with no running event loop, so ``asyncio.run``
    here opens the only loop involved -- and everything loop-bound is opened and
    closed inside it. The runner path keeps its own long-lived producer; it does
    not come through this class.
    """

    def __init__(self, *, bootstrap_servers: str) -> None:
        self._bootstrap_servers = bootstrap_servers.strip()

    def publish(self, message: ModelSnapshotDeltaMessage) -> bool:
        if not self._bootstrap_servers:
            logger.warning(
                "snapshot delta for %s not published: no Kafka brokers resolved "
                "for this process",
                message.topic,
            )
            return False
        return asyncio.run(self._publish(message))

    async def _publish(self, message: ModelSnapshotDeltaMessage) -> bool:
        # Lazy imports (OMN-15800 AC6): the projection-api process imports names
        # from the projection package but must never load asyncpg, which
        # ``omnibase_infra``'s top-level __init__ chain pulls in transitively.
        from aiokafka import AIOKafkaProducer
        from omnibase_infra.event_bus.kafka_auth import (
            build_aiokafka_auth_kwargs_from_env,
        )

        producer = AIOKafkaProducer(
            bootstrap_servers=self._bootstrap_servers,
            # None must pass through unchanged: aiokafka calls the serializer
            # unconditionally, including for a tombstone publish (value=None),
            # so a naive ``v.encode("utf-8")`` would raise on None.
            value_serializer=lambda v: (
                v if v is None or isinstance(v, bytes) else v.encode("utf-8")
            ),
            **build_aiokafka_auth_kwargs_from_env(),
        )
        try:
            await producer.start()
        except Exception as exc:
            logger.warning(
                "snapshot delta for %s not published: producer failed to start: %s",
                message.topic,
                exc,
            )
            return False
        try:
            await producer.send_and_wait(
                message.topic,
                value=message.value,
                key=message.key,
                headers=list(message.headers),
            )
        except Exception as exc:
            logger.warning(
                "snapshot delta for %s not published: send failed: %s",
                message.topic,
                exc,
            )
            return False
        finally:
            await producer.stop()
        return True
