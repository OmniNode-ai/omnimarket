# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The host disk workflow chain over a wire (OMN-23866): reading, scheduled run, command, effect result, run record.

The wire is the bus the chain's events travel on. ``InMemoryWire`` is the in-memory bus; ``DevBrokerWire`` is the real
dev broker, opened the way every deployed node opens its lane (the lane named by HOST_DISK_CHAIN_LANE under the
workspace root HOST_DISK_CHAIN_ROOT) and read back under a group unique to the run. Every handler input is parsed from
the bytes the wire carried, so the chain holds each seam: what one side publishes is what the next validates.

The host disk effect is not installed in this step, so a stand-in answers each command: it completes, refuses, or stays
silent. Nothing else in the chain is a stand-in: the schedule gate, the run state, the decision compute and the
orchestrator's handlers are the real ones.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import uuid
from pathlib import Path
from typing import Any, Literal, Protocol

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput
from pydantic import BaseModel

from omnimarket.models.model_host_disk_orchestrator import (
    EnumHostDiskFailureReason,
    ModelHostDiskEvictCompleted,
    ModelHostDiskEvictFailed,
    ModelHostDiskEvictRequested,
    ModelHostDiskRunCompleted,
)
from omnimarket.models.model_host_disk_reading import ModelHostDiskRead
from tests.nodes.node_host_disk_orchestrator.orchestrator_inputs import (
    COMPLETED_TOPIC,
    FAILED_TOPIC,
    HOSTS,
    READ_TOPIC,
    REQUESTED_TOPIC,
    RUN_TOPIC,
    SLACK_TOPIC,
    T0,
    Orchestrator,
    completed_for,
    failed_for,
    read_event,
    tick,
)

LANE_ENV, ROOT_ENV = "HOST_DISK_CHAIN_LANE", "HOST_DISK_CHAIN_ROOT"
Effect = Literal["complete", "refuse", "silent"]
CHAIN_TOPICS = (
    READ_TOPIC,
    REQUESTED_TOPIC,
    COMPLETED_TOPIC,
    FAILED_TOPIC,
    SLACK_TOPIC,
    RUN_TOPIC,
)


class Wire(Protocol):
    async def publish(self, topic: str, event: BaseModel, key: str) -> None: ...

    async def read(
        self, topic: str, key: str, at_least: int
    ) -> list[dict[str, Any]]: ...


class InMemoryWire:
    def __init__(self) -> None:
        self._bus = EventBusInmemory()
        self._started = False

    async def publish(self, topic: str, event: BaseModel, key: str) -> None:
        if not self._started:
            await self._bus.start()
            self._started = True
        await self._bus.publish(
            topic, key=key.encode(), value=event.model_dump_json().encode()
        )

    async def read(self, topic: str, key: str, at_least: int) -> list[dict[str, Any]]:
        history = await self._bus.get_event_history(limit=100000)
        return [
            json.loads(bytes(m.value))
            for m in history
            if m.topic == topic and bytes(m.key or b"").decode() == key
        ]


class DroppingWire(InMemoryWire):
    """The injected break: accepts every publish on the dropped topics and delivers nothing."""

    def __init__(self, dropped: frozenset[str]) -> None:
        super().__init__()
        self._dropped = dropped

    async def publish(self, topic: str, event: BaseModel, key: str) -> None:
        if topic in self._dropped:
            return
        await super().publish(topic, event, key)


class DevBrokerWire:
    """The real dev broker: publish with the lane bus, read back from a consumer group unique to the run."""

    def __init__(self, lane: str, root: Path, key: str) -> None:
        self._lane, self._root, self._key = lane, root, key
        self._seen: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self._ctx: Any = None
        self._bus: Any = None

    async def __aenter__(self) -> DevBrokerWire:
        from omnimarket.handlers.overlay_store.bus_opener import open_lane_bus
        from omnimarket.topic_namespace import apply_topic_namespace

        self._ctx = open_lane_bus(
            bus="kafka", lane=self._lane, kafka_bootstrap=None, root=self._root
        )
        self._bus = await self._ctx.__aenter__()
        namespaced = {apply_topic_namespace(t): t for t in CHAIN_TOPICS}

        async def collect(message: Any) -> None:
            topic = namespaced.get(getattr(message, "topic", ""), "")
            key = bytes(getattr(message, "key", b"") or b"").decode()
            self._seen.setdefault((topic, key), []).append(
                json.loads(bytes(message.value))
            )

        for namespaced_topic in namespaced:
            await self._bus.subscribe(
                namespaced_topic,
                on_message=collect,
                group_id=f"host-disk-chain-{self._key}",
            )
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._ctx.__aexit__(*exc)

    async def publish(self, topic: str, event: BaseModel, key: str) -> None:
        from omnimarket.topic_namespace import apply_topic_namespace

        await self._bus.publish(
            apply_topic_namespace(topic), key.encode(), event.model_dump_json().encode()
        )

    async def read(self, topic: str, key: str, at_least: int) -> list[dict[str, Any]]:
        for _ in range(60):
            if len(self._seen.get((topic, key), [])) >= at_least:
                break
            await asyncio.sleep(1)
        return list(self._seen.get((topic, key), []))


def dev_broker_target() -> tuple[str, Path]:
    lane, root = os.environ.get(LANE_ENV), os.environ.get(ROOT_ENV)
    if not (lane and root):
        pytest.skip(
            f"Kafka not reachable: {LANE_ENV} and {ROOT_ENV} name no declared bus lane"
        )
    return lane, Path(root)


async def publish_output(
    wire: Wire, output: ModelHandlerOutput[None], key: str
) -> None:
    for envelope in output.events:
        assert isinstance(envelope.payload, BaseModel)
        await wire.publish(str(envelope.event_type), envelope.payload, key)


async def run_chain(
    wire: Wire,
    orch: Orchestrator,
    key: str,
    *,
    effect: Effect = "complete",
    read_hosts: tuple[str, ...] = HOSTS,
    free_bytes: int = 500 * 1024**3,
    stale_revisions: int = 2,
    reading_age: dt.timedelta = dt.timedelta(0),
) -> list[ModelHostDiskRunCompleted]:
    """Reading published and read back, one scheduled run, the stand-in effect's answers, and the run records."""
    for host in read_hosts:
        await wire.publish(
            READ_TOPIC,
            read_event(
                host,
                read_at=T0 - reading_age,
                free_bytes=free_bytes,
                stale_revisions=stale_revisions,
            ),
            key,
        )
    for raw in await wire.read(READ_TOPIC, key, len(read_hosts)):
        orch.read(ModelHostDiskRead.model_validate(raw))
    started = orch.scheduled.handle(tick(T0))
    await publish_output(wire, started, key)
    # a run with nothing to send records itself at once; otherwise its commands are on the wire
    sent = sum(
        isinstance(e.payload, ModelHostDiskEvictRequested) for e in started.events
    )
    requested = [
        ModelHostDiskEvictRequested.model_validate(raw)
        for raw in await wire.read(REQUESTED_TOPIC, key, sent)
    ]
    for request in requested:
        if effect == "complete":
            await wire.publish(COMPLETED_TOPIC, completed_for(request), key)
        elif effect == "refuse":
            await wire.publish(
                FAILED_TOPIC,
                failed_for(request, EnumHostDiskFailureReason.REFUSED),
                key,
            )
    answered = len(requested) if effect != "silent" else 0
    topic, model = (
        (COMPLETED_TOPIC, ModelHostDiskEvictCompleted)
        if effect == "complete"
        else (FAILED_TOPIC, ModelHostDiskEvictFailed)
    )
    if answered:
        handle = orch.completed.handle if effect == "complete" else orch.failed.handle
        for raw in await wire.read(topic, key, answered):
            await publish_output(wire, handle(model.model_validate(raw)), key)
    if effect == "silent":
        late = T0 + dt.timedelta(seconds=orch.overlay.result_deadline_seconds)
        await publish_output(wire, orch.scheduled.handle(tick(late)), key)
    return [
        ModelHostDiskRunCompleted.model_validate(raw)
        for raw in await wire.read(RUN_TOPIC, key, 1)
    ]


def new_key() -> str:
    return uuid.uuid4().hex
