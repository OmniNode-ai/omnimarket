# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Store each loaded overlay or set, then publish what the store holds.

The snapshot is the store read back after the commit, so a reader on the bus sees what the database
holds and nothing else, and one whole record per overlay. A malformed load raises, which the runtime
dead-letters, and the stored overlay stays as it was.

A set (a write that names a write id, the revision it was built on and its audit row) is applied by the store
only on that revision, in one transaction with its provenance and its audit row. The audit topic then carries the
row read from the table, and the reply topic carries the committed or conflict answer under the write id. A
delivery of a write id that was applied before is answered again and applies nothing; it publishes the audit row
only if an earlier delivery did not.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import yaml

from omnimarket.handlers.handler_projection_runtime_writer import (
    RuntimeProjectionWriter,
)
from omnimarket.handlers.overlay_store.store_postgres import PostgresOverlayStore
from omnimarket.models.overlay_store import (
    EnumOverlayWriteOutcome,
    ModelOverlayStoreRecord,
    ModelOverlayStoreWrite,
)
from omnimarket.projection.envelope import strip_runner_injected_keys
from omnimarket.projection.runner import BaseProjectionRunner, MessageMeta, PublishFn

PublishKeyedFn = Callable[[str, bytes | None, bytes], Coroutine[Any, Any, None]]
StoreFactory = Callable[[Any], Any]

_STORE_TABLE = "overlay_store"
_SNAPSHOT_MARK = "onex.snapshot."


class HandlerOverlayStoreProjectionRunner(BaseProjectionRunner):
    """One delivery: validate, store, read back, publish."""

    def __init__(
        self,
        contract_path: Path | None = None,
        *,
        publish_fn: PublishFn | None = None,
        publish_keyed_fn: PublishKeyedFn | None = None,
        store_factory: StoreFactory | None = None,
    ) -> None:
        super().__init__(publish_fn=publish_fn)
        self._publish_keyed_fn = publish_keyed_fn
        self._store_factory = store_factory
        path = contract_path or Path(__file__).parent.parent / "contract.yaml"
        with open(path) as handle:
            self._contract: dict[str, Any] = yaml.safe_load(handle)
        published = self._contract["event_bus"]["publish_topics"]
        self._snapshot_topic = next(
            t for t in published if t.startswith(_SNAPSHOT_MARK)
        )
        write_topics = self._contract["write_topics"]
        self._audit_topic: str = write_topics["audit"]
        self._reply_topic: str = write_topics["write_reply"]

    @property
    def subscribe_topics(self) -> list[str]:
        return list(self._contract["event_bus"]["subscribe_topics"])

    @property
    def topics(self) -> list[str]:
        return self.subscribe_topics

    @property
    def poison_dlq_topics(self) -> list[str]:
        return list(self._contract["event_bus"]["dlq_topics"])

    async def publish_dlq(self, topic: str, value: bytes) -> None:
        publish = await self.get_publish_fn()
        if publish is None:
            raise RuntimeError(f"no DLQ publisher configured for {topic}")
        await publish(topic, value)

    async def get_keyed_publish_fn(self) -> PublishKeyedFn | None:
        """Return the keyed publish callable: the injected one, else one over the shared producer.

        Returns ``None`` when no brokers are configured. As in ``get_publish_fn``, the callable
        resolves the live producer on every call, because the runtime host stops the producer
        after each delivery.
        """
        if self._publish_keyed_fn is not None:
            return self._publish_keyed_fn
        if await self._ensure_producer() is None:
            return None

        async def _publish_keyed(topic: str, key: bytes | None, value: bytes) -> None:
            if (live := await self._ensure_producer()) is None:
                raise RuntimeError("no Kafka producer available to publish")
            await live.send_and_wait(topic, value, key=key)

        return _publish_keyed

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        payload = strip_runner_injected_keys(data)
        payload.pop("_db", None)
        write = ModelOverlayStoreWrite.model_validate(payload)
        # Looked up at the call, so a test that replaces the module's store class reaches it.
        make_store = self._store_factory or PostgresOverlayStore
        store = make_store(self.db_for(_STORE_TABLE, operation="write").pool)
        reply = await store.apply_write(write)
        current = await store.read_current(write.overlay_id)
        publish = await self.get_keyed_publish_fn()
        if publish is None:
            return True
        committed = reply.outcome is EnumOverlayWriteOutcome.COMMITTED
        if write.write_id is not None and committed:
            # The audit topic carries the row the table holds, once: a delivery after a failed publish sends it.
            audit = await store.pending_audit(write.write_id)
            if audit is not None:
                await publish(
                    self._audit_topic,
                    f"{audit.overlay_id}:{audit.key}".encode(),
                    audit.model_dump_json().encode("utf-8"),
                )
                await store.mark_audit_published(write.write_id)
        if current is not None:
            await publish(
                self._snapshot_topic,
                write.overlay_id.encode("utf-8"),
                ModelOverlayStoreRecord.model_validate(current)
                .model_dump_json()
                .encode("utf-8"),
            )
        if write.write_id is not None:
            await publish(
                self._reply_topic,
                write.write_id.encode("utf-8"),
                reply.model_dump_json().encode("utf-8"),
            )
        return True


class OverlayStoreProjectionWriter(RuntimeProjectionWriter):
    """Runtime dispatch entrypoint: apply the runner once for each delivery."""

    runner_type = HandlerOverlayStoreProjectionRunner


__all__ = ["HandlerOverlayStoreProjectionRunner", "OverlayStoreProjectionWriter"]
