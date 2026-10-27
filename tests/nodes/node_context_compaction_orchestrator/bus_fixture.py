# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""In-memory transport using real runtime publishing, dispatch and terminal routing."""

from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import Future, TimeoutError
from pathlib import Path
from typing import Any, cast

from omnibase_core.enums.enum_node_kind import EnumNodeKind
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_core.event_bus.event_bus_semantic_fake import EventBusSemanticFake
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import (
    _make_dispatch_callback,
    _make_event_bus_callback,
    _make_sync_event_publisher,
)
from omnibase_infra.runtime.contract_terminal_events import (
    declared_failure_terminal_topics,
)
from omnibase_infra.runtime.service_dispatch_result_applier import (
    build_contract_result_applier,
)

from omnimarket.models.context_pack_orchestrator import ModelContextPackBuilt
from omnimarket.models.model_session_stream_barrier import (
    ModelSessionStreamBarrierProbe,
)
from omnimarket.nodes.node_context_compaction_orchestrator.handlers.compaction_runtime_binding import (
    load_compaction_pack_topics,
    load_compaction_runtime_binding,
)
from omnimarket.nodes.node_context_compaction_orchestrator.handlers.handler_context_compaction_orchestrator import (
    HandlerContextCompactionOrchestrator,
)

NODES = Path(__file__).resolve().parents[3] / "src/omnimarket/nodes"


class MetSource:
    def read(self, session_id: str, barrier_id: str) -> ModelSessionStreamBarrierProbe:
        from .test_session_barrier_wait import _probe

        probe = _probe((1, 2, 3), (1, 2, 3))
        return probe.model_copy(
            update={
                "session_id": session_id,
                "barrier_id": barrier_id,
                "barriers": tuple(
                    b.model_copy(update={"barrier_id": barrier_id})
                    for b in probe.barriers
                ),
                "source_revision": "2026-10-23T12:00:00Z",
            }
        )


class CannedPack:
    def __init__(self, built: ModelContextPackBuilt) -> None:
        self.built = built
        self.commands: list[Any] = []

    def handle(self, command: Any) -> ModelContextPackBuilt:
        self.commands.append(command)
        return self.built


class MemorySession:
    def __init__(self, consumer: MemoryConsumer, topic: str) -> None:
        self.consumer = consumer
        self.topic = topic

    def register(self, correlation_id: str) -> None:
        with self.consumer.lock:
            self.consumer.pending.setdefault(correlation_id, Future())
            self.consumer.registrations.append((self.topic, correlation_id))

    def wait(
        self, correlation_id: str, timeout_seconds: float
    ) -> dict[str, object] | None:
        self.consumer.waits.append((correlation_id, timeout_seconds))
        try:
            return self.consumer.pending[correlation_id].result(timeout_seconds)
        except TimeoutError:
            return None

    def close(self) -> None:
        pass


class MemoryConsumer:
    def __init__(
        self,
        bus: EventBusInmemory | EventBusSemanticFake,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self.bus = bus
        self.loop = loop
        self.lock = threading.Lock()
        self.pending: dict[str, Future[dict[str, object]]] = {}
        self.opened: set[str] = set()
        self.registrations: list[tuple[str, str]] = []
        self.waits: list[tuple[str, float]] = []

    def open(self, topic: str) -> MemorySession:
        async def subscribe() -> None:
            if topic in self.opened:
                return

            async def receive(message: Any) -> None:
                body = json.loads(message.value)
                with self.lock:
                    future = self.pending.get(str(body.get("correlation_id")))
                    if future is not None and not future.done():
                        future.set_result(body)

            await self.bus.subscribe(
                topic, group_id="terminal-fixture", on_message=receive
            )
            self.opened.add(topic)

        asyncio.run_coroutine_threadsafe(subscribe(), self.loop).result(5)
        return MemorySession(self, topic)


class BusHarness:
    """A loop on a worker thread also supports synchronous legacy test entrypoints."""

    def __init__(
        self,
        pack: Any = None,
        *,
        silent: bool = False,
        bus_class: Any = EventBusInmemory,
    ) -> None:
        self.bus_class = bus_class
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self.pack = pack
        self.silent = silent
        self.records: list[tuple[str, dict[str, Any]]] = []
        asyncio.run_coroutine_threadsafe(self._start(), self.loop).result(10)

    async def _start(self) -> None:
        self.bus = self.bus_class(environment="local", group="compaction-fixture")
        await self.bus.start()
        self.topics = load_compaction_pack_topics()
        self.consumer = MemoryConsumer(self.bus, self.loop)
        self.publisher = _make_sync_event_publisher(
            event_bus=self.bus, handler_name="compaction-fixture"
        )
        manifest = discover_contracts_from_paths(
            [NODES / "node_context_pack_orchestrator/contract.yaml"]
        )
        assert not manifest.errors
        (contract,) = manifest.contracts
        assert contract.handler_routing is not None
        assert contract.event_bus is not None
        (route,) = contract.handler_routing.handlers
        if self.pack is None and not self.silent:
            from .test_compaction_pack_build import _pack_orchestrator, _sources

            self.pack = _pack_orchestrator(_sources())
        callback = (
            _make_dispatch_callback(
                self.pack,
                event_model=route.event_model,
                handler_node_kind=EnumNodeKind.ORCHESTRATOR,
            )
            if not self.silent
            else None
        )
        self.applier = build_contract_result_applier(
            event_bus=cast(Any, self.bus),
            contract_path=NODES / "node_context_pack_orchestrator/contract.yaml",
            publish_topics=contract.event_bus.publish_topics,
        )

        class PackDispatch:
            async def dispatch_scoped(self, topic, envelope, *, allowed_dispatcher_ids):
                assert allowed_dispatcher_ids == frozenset({"pack-fixture"})
                assert callback is not None
                return await callback(envelope)

        boundary = _make_event_bus_callback(
            self.topics.request_topic,
            cast(Any, PackDispatch()),
            self.applier,
            event_bus=self.bus,
            allowed_dispatcher_ids={"pack-fixture"},
            failure_terminal_topics=declared_failure_terminal_topics(
                NODES / "node_context_pack_orchestrator/contract.yaml",
                success_topic=self.topics.completed_topic,
                publishable_topics=contract.event_bus.publish_topics,
            ),
        )

        async def record(message: Any) -> None:
            self.records.append((message.topic, json.loads(message.value)))

        async def answer(message: Any) -> None:
            body = json.loads(message.value)
            cid = body["correlation_id"]
            # Both terminals must already have a registered waiter when the command arrives.
            assert (self.topics.completed_topic, cid) in self.consumer.registrations
            assert (self.topics.failed_topic, cid) in self.consumer.registrations
            if callback is not None:
                await boundary(message)

        for topic in (
            self.topics.request_topic,
            self.topics.completed_topic,
            self.topics.failed_topic,
        ):
            await self.bus.subscribe(
                topic, group_id="record-fixture", on_message=record
            )
        await self.bus.subscribe(
            self.topics.request_topic, group_id="pack-fixture", on_message=answer
        )

    def handler(self, **kwargs: Any) -> HandlerContextCompactionOrchestrator:
        if "binding" not in kwargs:
            kwargs["binding"] = load_compaction_runtime_binding().model_copy(
                update={"tenant_id": "tenant-a"}
            )
        kwargs.setdefault("barrier_probe_source", MetSource())
        kwargs.setdefault("event_publisher", self.publisher)
        kwargs.setdefault("event_consumer", self.consumer)
        return HandlerContextCompactionOrchestrator(**kwargs)

    @property
    def commands(self) -> list[dict[str, Any]]:
        return [
            body for topic, body in self.records if topic == self.topics.request_topic
        ]

    def close(self) -> None:
        asyncio.run_coroutine_threadsafe(self.bus.close(), self.loop).result(5)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)
        self.loop.close()
