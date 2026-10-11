# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: node_lab_job_check_schedule_compute turns the runtime tick into a sweep request.

The effect (node_lab_job_check_effect) subscribes to the request topic, and the
schedule node is its only producer. The contracts are read from disk and the
schedule is booted through ``wire_from_manifest``, the call the kernel makes,
with an in-memory bus; the handler, dispatch engine and contract wiring are the
runtime's own.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
from omnibase_infra.runtime.auto_wiring.models import ModelAutoWiringManifest
from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.validators.contract_topic_graph import GRAPH_PACKAGES, build_graph

pytestmark = pytest.mark.unit

NODES = Path(__file__).parents[1] / "src" / "omnimarket" / "nodes"
SCHEDULE_NODE = "node_lab_job_check_schedule_compute"
EFFECT_NODE = "node_lab_job_check_effect"
TICK_TOPIC = "onex.intent.platform.runtime-tick.v1"
REQUEST_TOPIC = "onex.cmd.omnimarket.lab-job-check-requested.v1"
DAY = dt.datetime(2026, 10, 11, tzinfo=dt.UTC)


def _contract(node: str) -> dict[str, Any]:
    loaded = yaml.safe_load((NODES / node / "contract.yaml").read_text())
    assert isinstance(loaded, dict)
    return loaded


def test_the_request_topic_has_the_schedule_node_as_its_producer(
    tmp_path: Path,
) -> None:
    roots = {package: tmp_path / package for package in GRAPH_PACKAGES}
    for root in roots.values():
        root.mkdir(parents=True, exist_ok=True)
    roots["omnimarket"] = NODES.parent
    graph = build_graph(roots=roots)
    assert any(SCHEDULE_NODE in p for p in graph.producers.get(REQUEST_TOPIC, ()))
    assert any(EFFECT_NODE in c for c in graph.consumers.get(REQUEST_TOPIC, ()))


def test_the_schedule_publishes_exactly_the_request_topic_the_effect_subscribes_to() -> (
    None
):
    bus = _contract(SCHEDULE_NODE)["event_bus"]
    assert bus["subscribe_topics"] == [TICK_TOPIC]
    assert bus["publish_topics"] == [REQUEST_TOPIC]
    assert _contract(EFFECT_NODE)["event_bus"]["subscribe_topics"] == [REQUEST_TOPIC]


def _tick_wire(now: dt.datetime) -> bytes:
    tick = ModelRuntimeTick(
        now=now,
        tick_id=uuid4(),
        sequence_number=1,
        scheduled_at=now,
        correlation_id=uuid4(),
        scheduler_id="golden-chain",
        tick_interval_ms=1000,
    )
    envelope: ModelEventEnvelope[object] = ModelEventEnvelope(
        payload=tick.model_dump(mode="json"),
        correlation_id=tick.correlation_id,
        envelope_timestamp=now,
    )
    return envelope.model_dump_json().encode("utf-8")


async def _drive(now: dt.datetime, *, wait_seconds: float) -> list[bytes]:
    command_topic = _contract(EFFECT_NODE)["runtime_dispatch"]["command_topic"]
    assert command_topic == REQUEST_TOPIC
    manifest = discover_contracts_from_paths([NODES / SCHEDULE_NODE / "contract.yaml"])
    assert not manifest.errors, manifest.errors
    bus = EventBusInmemory(environment="lab-job-check", group="lab-job-check")
    await bus.start()
    seen: list[bytes] = []
    got = asyncio.Event()

    def _collector() -> Callable[[object], Awaitable[None]]:
        async def _collect(message: object) -> None:
            seen.append(bytes(getattr(message, "value", b"")))
            got.set()

        return _collect

    await bus.subscribe(command_topic, on_message=_collector(), group_id="probe")
    engine = MessageDispatchEngine()
    report = await wire_from_manifest(
        ModelAutoWiringManifest(contracts=tuple(manifest.contracts)),
        engine,
        event_bus=bus,
        environment="local",
    )
    assert report.total_failed == 0
    assert report.total_wired == 1
    engine.freeze()
    await bus.publish(TICK_TOPIC, None, _tick_wire(now), None)
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(got.wait(), timeout=wait_seconds)
    await bus.close()
    return seen


async def test_a_window_tick_publishes_one_sweep_request_through_the_runtime() -> None:
    seen = await _drive(DAY + dt.timedelta(hours=3), wait_seconds=30)
    assert len(seen) == 1
    assert b"nonterminal" in seen[0]


async def test_an_off_window_tick_publishes_nothing_through_the_runtime() -> None:
    seen = await _drive(DAY + dt.timedelta(hours=3, seconds=90), wait_seconds=2)
    assert seen == []
