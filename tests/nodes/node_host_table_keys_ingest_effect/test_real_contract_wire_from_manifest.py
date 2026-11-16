# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The real ingest contract and handler dispatched through wire_from_manifest (injected store and overlay).

The companion test in the runtime repository wires stand-ins and a topic_match contract. This one wires the
real contract.yaml (payload_type_match) and the real HandlerHostTableKeysIngest, so a change to either side that
brings back an undeliverable result, or stops the handler running under the runtime, turns it red.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.runtime.auto_wiring import handler_wiring
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine

from omnimarket.models.host_table_keys import ModelHostTableKeysDocument
from omnimarket.nodes.node_host_table_keys_ingest_effect.handlers.handler_host_table_keys_ingest import (
    HandlerHostTableKeysIngest,
)
from omnimarket.nodes.node_host_table_keys_ingest_effect.handlers.store_host_keys import (
    StoreHostKeysInMemory,
)
from tests.nodes.node_host_table_compute.table_support import three_hosts
from tests.nodes.node_host_table_keys_ingest_effect.support import (
    NOW,
    PREFIX,
    overlay,
    record,
    registry_document,
)

pytestmark = pytest.mark.unit

TOPIC = "onex.evt.omnimarket.overlay-store-loaded.v1"


def _contract_path() -> Path:
    package = importlib.import_module(
        "omnimarket.nodes.node_host_table_keys_ingest_effect"
    )
    assert package.__file__ is not None
    return Path(package.__file__).parent / "contract.yaml"


async def _dispatch(handler_class: type[HandlerHostTableKeysIngest]) -> AsyncMock:
    """Wire the real contract with handler_class standing in for the bare build, publish one record, return the route mock."""
    manifest = discover_contracts_from_paths([_contract_path()])
    assert not manifest.errors, manifest.errors
    assert len(manifest.contracts) == 1
    real_import = handler_wiring._import_handler_class

    def import_handler(module_path: str, class_name: str) -> type:
        if class_name == "HandlerHostTableKeysIngest":
            return handler_class
        return real_import(module_path, class_name)

    bus = EventBusInmemory(environment="test", group="omn24721-real-contract")
    await bus.start()
    try:
        engine = MessageDispatchEngine()
        with (
            patch.object(handler_wiring, "_import_handler_class", import_handler),
            patch.object(
                handler_wiring,
                "_route_apply_publish_failure",
                new_callable=AsyncMock,
            ) as route_failure,
        ):
            report = await handler_wiring.wire_from_manifest(
                manifest, engine, event_bus=bus, environment="local"
            )
            assert report.total_wired == 1, report.model_dump()
            engine.freeze()
            payload: dict[str, Any] = record(
                registry_document(*three_hosts())
            ).model_dump(mode="json")
            envelope: ModelEventEnvelope[object] = ModelEventEnvelope(
                payload=payload,
                correlation_id=uuid4(),
                event_type="omnimarket.overlay-store-loaded",
            )
            await bus.publish(
                TOPIC, None, envelope.model_dump_json().encode("utf-8"), None
            )
        return route_failure
    finally:
        await bus.close()


def _injected(store: StoreHostKeysInMemory) -> type[HandlerHostTableKeysIngest]:
    class Injected(HandlerHostTableKeysIngest):
        def __init__(self) -> None:
            super().__init__(store=store, overlay=overlay(), clock=lambda: NOW)

    return Injected


async def test_the_real_contract_and_handler_dispatch_without_an_undeliverable_result() -> (
    None
):
    store = StoreHostKeysInMemory()
    route_failure = await _dispatch(_injected(store))
    route_failure.assert_not_awaited()
    stored = store.get(f"{PREFIX}:table")
    assert stored is not None, "the handler did not run under the runtime"
    document = ModelHostTableKeysDocument.model_validate_json(stored)
    assert len(document.table.rows) == 3


async def test_a_handler_that_raises_leaves_no_key_so_the_dispatch_is_really_the_handler() -> (
    None
):
    store = StoreHostKeysInMemory()
    calls: list[int] = []

    class Raising(_injected(store)):  # type: ignore[misc]
        def handle(self, request: Any) -> None:
            calls.append(1)
            raise RuntimeError("handler body replaced")

    await _dispatch(Raising)
    assert calls == [1]
    assert store.get(f"{PREFIX}:table") is None
