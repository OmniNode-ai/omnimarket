# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Store each loaded overlay, then publish what the store holds.

The snapshot is the store read back after the commit, so a reader on the bus sees what the database
holds and nothing else, and one whole record per overlay. A malformed load raises, which the runtime
dead-letters, and the stored overlay stays as it was.
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
from omnimarket.models.overlay_store import ModelOverlayStoreRecord
from omnimarket.projection.envelope import strip_runner_injected_keys
from omnimarket.projection.runner import BaseProjectionRunner, MessageMeta, PublishFn

PublishKeyedFn = Callable[[str, bytes | None, bytes], Coroutine[Any, Any, None]]

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
    ) -> None:
        super().__init__(publish_fn=publish_fn)
        self._publish_keyed_fn = publish_keyed_fn
        path = contract_path or Path(__file__).parent.parent / "contract.yaml"
        with open(path) as handle:
            self._contract: dict[str, Any] = yaml.safe_load(handle)
        published = self._contract["event_bus"]["publish_topics"]
        self._snapshot_topic = next(
            t for t in published if t.startswith(_SNAPSHOT_MARK)
        )

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
        loaded = ModelOverlayStoreRecord.model_validate(payload)
        store = PostgresOverlayStore(self.db_for(_STORE_TABLE, operation="write").pool)
        await store.write_version(loaded.model_dump(mode="json"))
        current = await store.read_current(loaded.overlay_id)
        publish = await self.get_keyed_publish_fn()
        if publish is not None and current is not None:
            await publish(
                self._snapshot_topic,
                loaded.overlay_id.encode("utf-8"),
                ModelOverlayStoreRecord.model_validate(current)
                .model_dump_json()
                .encode("utf-8"),
            )
        return True


class OverlayStoreProjectionWriter(RuntimeProjectionWriter):
    """Runtime dispatch entrypoint: apply the runner once for each delivery."""

    runner_type = HandlerOverlayStoreProjectionRunner


__all__ = ["HandlerOverlayStoreProjectionRunner", "OverlayStoreProjectionWriter"]
