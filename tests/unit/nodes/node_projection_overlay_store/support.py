# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The projection and the set command wired through one in-process loopback, over the in-memory store.

``Loopback`` plays the broker between the two: a write the set command publishes on the loaded topic is delivered to the
projection's runner; the runner's snapshot, audit row and reply come back to the set command's reader and reply waiter.
The store is the in-memory store, which keeps the same rules as the Postgres one (the real-Postgres test runs those rules
against the SQL).
"""

from __future__ import annotations

import asyncio
import json
import types
from collections import defaultdict
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from importlib import resources
from typing import Any, Literal

import yaml
from pydantic import JsonValue

from omnimarket.handlers.overlay_store.store_in_memory import InMemoryOverlayStore
from omnimarket.models.overlay_store import (
    ModelOverlaySettingAudit,
    ModelOverlayStoreRecord,
    ModelOverlayStoreWrite,
    ModelOverlayWriteReply,
    document_digest,
)
from omnimarket.nodes.node_overlay_setting_set_effect.handlers.handler_overlay_setting_set import (
    HandlerOverlaySettingSet,
    load_settable_settings,
)
from omnimarket.nodes.node_projection_overlay_store.handlers.handler_overlay_store_projection import (
    HandlerOverlayStoreProjectionRunner,
)
from omnimarket.projection.runner import MessageMeta

SET_NODE = "node_overlay_setting_set_effect"
PROJECTION_NODE = "node_projection_overlay_store"
OVERLAY_ID = "node_build_delegation_checks"
# One overlay that declares two settable keys; the document is the whole of what its model needs.
KEY_OWNER = "owner"
KEY_LANE = "alarm_lane"
PATH_OWNER = "owner"
PATH_LANE = "alarm_lane"
NOW = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
LOADED_AT = NOW - timedelta(minutes=20)
META = MessageMeta(topic="t", partition=0, offset=1, fallback_id="f")


def contract(node: str) -> dict[str, Any]:
    text = (
        resources.files(f"omnimarket.nodes.{node}")
        .joinpath("contract.yaml")
        .read_text(encoding="utf-8")
    )
    loaded: dict[str, Any] = yaml.safe_load(text)
    return loaded


LOADED_TOPIC: str = contract(SET_NODE)["event_bus"]["publish_topics"][0]
AUDIT_TOPIC: str = contract(PROJECTION_NODE)["write_topics"]["audit"]
REPLY_TOPIC: str = contract(PROJECTION_NODE)["write_topics"]["write_reply"]
SNAPSHOT_TOPIC: str = contract(PROJECTION_NODE)["event_bus"]["publish_topics"][0]


def default_document() -> dict[str, JsonValue]:
    return {"owner": "owner-a", "alarm_lane": "lane-a"}


def default_load(
    document: Mapping[str, JsonValue] | None = None,
    *,
    loaded_at: datetime = LOADED_AT,
) -> dict[str, Any]:
    doc = dict(default_document() if document is None else document)
    return ModelOverlayStoreRecord(
        overlay_id=OVERLAY_ID,
        digest=document_digest(doc),
        document=doc,
        source_ref="node_build_delegation_checks@abc123",
        loaded_by="loader",
        loaded_at=loaded_at,
    ).model_dump(mode="json", exclude={"operator_paths", "revision"})


class _Runner(HandlerOverlayStoreProjectionRunner):
    def db_for(
        self, table: str, *, operation: Literal["read", "write"] = "write"
    ) -> Any:
        return types.SimpleNamespace(pool=None)


class _Pending:
    def __init__(self, future: asyncio.Future[ModelOverlayWriteReply]) -> None:
        self._future = future

    async def result(self, timeout_s: float) -> ModelOverlayWriteReply:
        return await asyncio.wait_for(asyncio.shield(self._future), timeout_s)


class Loopback:
    """The broker between the set command and the projection, with the projection's store in memory."""

    def __init__(
        self,
        store: InMemoryOverlayStore | None = None,
        *,
        gate_first_reads: int = 0,
        drop_replies: int = 0,
        fail_publish_on: str | None = None,
    ) -> None:
        self.store = store or InMemoryOverlayStore()
        self.runner = _Runner(
            publish_keyed_fn=self._publish_keyed, store_factory=lambda _pool: self.store
        )
        self.topics: dict[str, list[tuple[bytes | None, bytes]]] = defaultdict(list)
        self.delivered: list[dict[str, Any]] = []
        self.reads = 0
        self.refreshes = 0
        # Every publish on the loaded and audit topics in order, and the reads, as the set command's tests read them.
        self.sequence: list[tuple[str, bytes | None, bytes]] = []
        self.log: list[str] = []
        self._snapshot: dict[str, dict[str, Any]] = {}
        self._waiting: dict[str, asyncio.Future[ModelOverlayWriteReply]] = {}
        self._gate = asyncio.Barrier(gate_first_reads) if gate_first_reads else None
        self._gate_reads = gate_first_reads
        self._drop_replies = drop_replies
        self._fail_publish_on = fail_publish_on
        self._tasks: list[asyncio.Task[bool]] = []

    async def _publish_keyed(self, topic: str, key: bytes | None, value: bytes) -> None:
        if topic == self._fail_publish_on:
            self._fail_publish_on = None
            raise ConnectionError(f"the broker refused {topic}")
        self.topics[topic].append((key, value))
        if topic == AUDIT_TOPIC:
            self.sequence.append((topic, key, value))
            self.log.append(f"publish:{topic}")
        if topic == SNAPSHOT_TOPIC:
            record = json.loads(value)
            self._snapshot[record["overlay_id"]] = record
        elif topic == REPLY_TOPIC:
            if self._drop_replies > 0:
                self._drop_replies -= 1
                return
            reply = ModelOverlayWriteReply.model_validate_json(value)
            waiting = self._waiting.get(str(reply.write_id))
            if waiting is not None and not waiting.done():
                waiting.set_result(reply)

    async def seed(self, load: Mapping[str, Any] | None = None) -> None:
        await self.runner.project_event(
            LOADED_TOPIC, dict(default_load() if load is None else load), META
        )

    async def deliver(self, payload: Mapping[str, Any]) -> bool:
        return await self.runner.project_event(LOADED_TOPIC, dict(payload), META)

    # The set command's bus, reader and reply waiter.
    async def publish(
        self, topic: str, key: bytes | None, value: bytes, headers: Any = None
    ) -> None:
        assert topic == LOADED_TOPIC, topic
        payload = json.loads(value)
        self.sequence.append((topic, key, value))
        self.log.append(f"publish:{topic}")
        self.delivered.append(payload)
        self._tasks.append(asyncio.create_task(self.deliver(payload)))

    async def read_current(self, overlay_id: str) -> Mapping[str, Any] | None:
        self.reads += 1
        self.log.append(f"read:{overlay_id}")
        if self._gate is not None and self.reads <= self._gate_reads:
            await self._gate.wait()
        return self._snapshot.get(overlay_id)

    async def refresh(self) -> None:
        self.refreshes += 1

    @asynccontextmanager
    async def expect(self, write_id: str) -> AsyncIterator[_Pending]:
        future: asyncio.Future[ModelOverlayWriteReply] = (
            asyncio.get_running_loop().create_future()
        )
        self._waiting[write_id] = future
        try:
            yield _Pending(future)
        finally:
            self._waiting.pop(write_id, None)

    def seed_snapshot(self, record: Mapping[str, Any]) -> None:
        """Put a record on the snapshot the set command reads, as the projection would have published it."""
        self._snapshot[str(record["overlay_id"])] = dict(record)

    def on(self, topic: str) -> list[dict[str, Any]]:
        return [json.loads(value) for _, value in self.topics[topic]]

    def snapshot(self, overlay_id: str = OVERLAY_ID) -> dict[str, Any]:
        return self._snapshot[overlay_id]

    def handler(
        self,
        *,
        max_attempts: int = 5,
        reply_timeout_s: float = 5.0,
        write_ids: list[str] | None = None,
        max_live_age_s: float = 7200.0,
        clock: Callable[[], datetime] = lambda: NOW,
    ) -> HandlerOverlaySettingSet:
        spec = contract(SET_NODE)
        ids = iter(write_ids or [])
        counter = iter(range(10_000))
        return HandlerOverlaySettingSet(
            reader=self,
            bus=self,
            replies=self,
            settings=load_settable_settings(spec),
            loaded_topic=LOADED_TOPIC,
            audit_topic=AUDIT_TOPIC,
            max_live_age_s=max_live_age_s,
            reply_timeout_s=reply_timeout_s,
            max_attempts=max_attempts,
            clock=clock,
            write_id_factory=lambda: next(ids, None) or f"w-{next(counter)}",
        )


def set_write(
    *,
    write_id: str = "w-set",
    based_on_revision: int = 0,
    key: str = KEY_OWNER,
    path: str = PATH_OWNER,
    value: str = "owner-b",
    actor: str = "ccp3-test",
    loaded_at: datetime = NOW,
    operator_paths: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """The write a set publishes, as plain data: what the projection receives from the bus."""
    document = default_document()
    previous = document[path]
    document[path] = value
    digest = document_digest(document)
    return ModelOverlayStoreWrite(
        overlay_id=OVERLAY_ID,
        digest=digest,
        document=document,
        source_ref="node_build_delegation_checks@abc123",
        loaded_by=actor,
        loaded_at=loaded_at,
        operator_paths=operator_paths or (path,),
        write_id=write_id,
        based_on_revision=based_on_revision,
        audit=ModelOverlaySettingAudit(
            overlay_id=OVERLAY_ID,
            key=key,
            setting_path=path,
            actor=actor,
            previous_present=True,
            previous_value=previous,
            new_value=value,
            document_digest=digest,
            set_at=loaded_at,
        ),
    ).model_dump(mode="json")
