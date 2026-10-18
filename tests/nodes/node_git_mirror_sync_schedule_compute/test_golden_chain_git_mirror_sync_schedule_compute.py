# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: the runtime tick asks node_git_query_mirror_effect for a detecting sync (OMN-19998).

node_git_mirror_sync_schedule_compute is the producer of the mirror's command
topic, and the mirror subscribes to it, so the runtime wires the mirror instead
of skipping it. The contracts are read from disk and booted through
``wire_from_manifest``, the call the kernel makes, with an in-memory bus.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
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

from omnimarket.models.model_runtime_tick_schedule import (
    ModelRuntimeTickScheduleConfig,
)
from omnimarket.nodes.node_git_mirror_sync_schedule_compute.handlers import (
    HandlerGitMirrorSyncSchedule,
    ModelGitMirrorSyncScheduleConfig,
    schedule_config,
)
from omnimarket.nodes.node_git_query_mirror_effect.models.model_git_query_mirror import (
    EnumGitQueryOperation,
    ModelGitQueryRequest,
)
from omnimarket.validators.contract_topic_graph import GRAPH_PACKAGES, build_graph

pytestmark = pytest.mark.unit

NODES = Path(__file__).parents[3] / "src" / "omnimarket" / "nodes"
SCHEDULE_NODE = "node_git_mirror_sync_schedule_compute"
MIRROR_NODE = "node_git_query_mirror_effect"
TICK_TOPIC = "onex.intent.platform.runtime-tick.v1"
REQUEST_TOPIC = "onex.cmd.omnimarket.git-query-mirror-requested.v1"
COMPLETED_TOPIC = "onex.evt.omnimarket.git-query-mirror-completed.v1"
PR_MERGED_TOPIC = "onex.evt.github.pr-merged.v1"
DAY = dt.datetime(2026, 10, 17, tzinfo=dt.UTC)


def _contract(node: str) -> dict[str, Any]:
    loaded = yaml.safe_load((NODES / node / "contract.yaml").read_text())
    assert isinstance(loaded, dict)
    return loaded


def _tick(now: dt.datetime) -> ModelRuntimeTick:
    return ModelRuntimeTick(
        now=now,
        tick_id=uuid4(),
        sequence_number=1,
        scheduled_at=now,
        correlation_id=uuid4(),
        scheduler_id="golden-chain",
        tick_interval_ms=1000,
    )


def _window_start() -> dt.datetime:
    cfg = schedule_config().schedule
    return DAY + dt.timedelta(seconds=cfg.offset_seconds)


def test_the_request_topic_has_the_schedule_as_producer_and_the_mirror_as_consumer(
    tmp_path: Path,
) -> None:
    roots = {package: tmp_path / package for package in GRAPH_PACKAGES}
    for root in roots.values():
        root.mkdir(parents=True, exist_ok=True)
    roots["omnimarket"] = NODES.parent
    graph = build_graph(roots=roots)
    assert any(SCHEDULE_NODE in p for p in graph.producers.get(REQUEST_TOPIC, ()))
    assert any(MIRROR_NODE in c for c in graph.consumers.get(REQUEST_TOPIC, ()))


def test_the_schedule_publishes_the_command_topic_the_mirror_subscribes_to() -> None:
    schedule = _contract(SCHEDULE_NODE)
    mirror = _contract(MIRROR_NODE)
    assert schedule["event_bus"]["subscribe_topics"] == [TICK_TOPIC]
    assert schedule["event_bus"]["publish_topics"] == [REQUEST_TOPIC]
    assert schedule["terminal_event"] == REQUEST_TOPIC
    assert mirror["runtime_dispatch"]["command_topic"] == REQUEST_TOPIC
    assert mirror["event_bus"]["subscribe_topics"] == [REQUEST_TOPIC]


def test_the_mirror_response_goes_to_the_completed_topic_not_the_pr_merged_topic() -> (
    None
):
    # The runtime publishes a handler's output on terminal_event when it is a
    # publish topic, else on the first publish topic, which is pr-merged here.
    mirror = _contract(MIRROR_NODE)
    assert mirror["terminal_event"] == COMPLETED_TOPIC
    assert COMPLETED_TOPIC in mirror["event_bus"]["publish_topics"]
    assert mirror["terminal_event"] != PR_MERGED_TOPIC


async def test_the_runtime_wires_the_mirror_to_its_command_topic() -> None:
    manifest = discover_contracts_from_paths([NODES / MIRROR_NODE / "contract.yaml"])
    assert not manifest.errors, manifest.errors
    bus = EventBusInmemory(environment="git-mirror-sync", group="git-mirror-sync")
    await bus.start()
    try:
        report = await wire_from_manifest(
            ModelAutoWiringManifest(contracts=tuple(manifest.contracts)),
            MessageDispatchEngine(),
            event_bus=bus,
            environment="local",
        )
    finally:
        await bus.close()
    assert report.total_failed == 0
    assert report.total_skipped == 0
    assert report.total_wired == 1


def test_a_window_tick_asks_for_a_detecting_sync_the_mirror_accepts() -> None:
    out = HandlerGitMirrorSyncSchedule().handle(_tick(_window_start()))
    assert out is not None
    request = ModelGitQueryRequest.model_validate(out.model_dump(mode="json"))
    assert request.operation is EnumGitQueryOperation.SYNC
    assert request.repo == "OmniNode-ai/omnimarket"
    assert request.detect_merges is True
    assert request.pr_number is None


def test_an_off_window_tick_asks_for_nothing() -> None:
    off = _window_start() + dt.timedelta(seconds=60)
    assert HandlerGitMirrorSyncSchedule().handle(_tick(off)) is None


def test_a_replayed_window_asks_for_the_same_sync() -> None:
    handler = HandlerGitMirrorSyncSchedule()
    first = handler.handle(_tick(_window_start()))
    again = handler.handle(_tick(_window_start()))
    assert first is not None
    assert again is not None
    assert first.correlation_id == again.correlation_id


def test_watched_repositories_take_turns_one_per_window() -> None:
    cfg = ModelGitMirrorSyncScheduleConfig(
        schedule=ModelRuntimeTickScheduleConfig(
            workflow="git-mirror-sync", interval_seconds=120, offset_seconds=0
        ),
        repos=("OmniNode-ai/omnimarket", "OmniNode-ai/omnibase_infra"),
    )
    handler = HandlerGitMirrorSyncSchedule(cfg)
    repos = []
    for window in range(4):
        out = handler.handle(_tick(DAY + dt.timedelta(seconds=120 * window)))
        assert out is not None
        repos.append(out.repo)
    assert repos == [
        "OmniNode-ai/omnimarket",
        "OmniNode-ai/omnibase_infra",
        "OmniNode-ai/omnimarket",
        "OmniNode-ai/omnibase_infra",
    ]


def _tick_wire(now: dt.datetime) -> bytes:
    tick = _tick(now)
    envelope: ModelEventEnvelope[object] = ModelEventEnvelope(
        payload=tick.model_dump(mode="json"),
        correlation_id=tick.correlation_id,
        envelope_timestamp=now,
    )
    return envelope.model_dump_json().encode("utf-8")


async def _drive(now: dt.datetime, *, wait_seconds: float) -> list[bytes]:
    manifest = discover_contracts_from_paths([NODES / SCHEDULE_NODE / "contract.yaml"])
    assert not manifest.errors, manifest.errors
    bus = EventBusInmemory(environment="git-mirror-sync", group="git-mirror-sync")
    await bus.start()
    seen: list[bytes] = []
    got = asyncio.Event()

    def _collector() -> Callable[[object], Awaitable[None]]:
        async def _collect(message: object) -> None:
            seen.append(bytes(getattr(message, "value", b"")))
            got.set()

        return _collect

    await bus.subscribe(REQUEST_TOPIC, on_message=_collector(), group_id="probe")
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


async def test_a_window_tick_publishes_one_sync_request_through_the_runtime() -> None:
    seen = await _drive(_window_start(), wait_seconds=30)
    assert len(seen) == 1
    payload = json.loads(seen[0])["payload"]
    request = ModelGitQueryRequest.model_validate(payload)
    assert request.operation is EnumGitQueryOperation.SYNC
    assert request.repo == "OmniNode-ai/omnimarket"


async def test_an_off_window_tick_publishes_nothing_through_the_runtime() -> None:
    seen = await _drive(_window_start() + dt.timedelta(seconds=60), wait_seconds=2)
    assert seen == []
