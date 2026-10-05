# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Runtime boot: a runtime tick reaches the lane-liveness drop alert.

Boots the gather, compute and drop-alert contracts through ``wire_from_manifest``
(the call the kernel makes), publishes a ``ModelRuntimeTick`` on the tick topic,
and reads the command, the evaluation and the Slack command off the bus. Only
the bus, the two database reads and the secret store are replaced; contracts,
handlers and dispatch are the runtime's own.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
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

from omnimarket.nodes.node_lane_liveness_drop_alert_effect.handlers import (
    handler_lane_liveness_drop_alert as alert_module,
)
from omnimarket.nodes.node_lane_liveness_gather_effect.handlers import (
    handler_lane_liveness_gather as gather_module,
)

pytestmark = pytest.mark.unit

NODES = Path(__file__).parents[1] / "src" / "omnimarket" / "nodes"
CONTRACTS = [
    NODES / "node_lane_liveness_gather_effect" / "contract.yaml",
    NODES / "node_lane_liveness_compute" / "contract.yaml",
    NODES / "node_lane_liveness_drop_alert_effect" / "contract.yaml",
]
TICK_TOPIC = "onex.intent.platform.runtime-tick.v1"
NOW = dt.datetime(2026, 10, 5, 14, 0, tzinfo=dt.UTC)


def _topics() -> tuple[str, str, str]:
    gather, compute, alert = (yaml.safe_load(p.read_text()) for p in CONTRACTS)
    return (
        gather["terminal_event"],
        compute["terminal_event"],
        alert["terminal_event"],
    )


def _tick_wire() -> bytes:
    tick = ModelRuntimeTick(
        now=NOW,
        tick_id=uuid4(),
        sequence_number=1,
        scheduled_at=NOW,
        correlation_id=uuid4(),
        scheduler_id="runtime-boot-test",
        tick_interval_ms=1000,
    )
    envelope: ModelEventEnvelope[object] = ModelEventEnvelope(
        payload=tick.model_dump(mode="json"),
        correlation_id=tick.correlation_id,
        envelope_timestamp=NOW,
    )
    return envelope.model_dump_json().encode("utf-8")


def _read(*, relay_last: dt.datetime) -> gather_module.ModelLaneWindowRead:
    return gather_module.ModelLaneWindowRead(
        lane_rows=(
            {
                "lane": "lane-dropped",
                "last_event_at": NOW - dt.timedelta(minutes=20),
                "event_count": 3,
            },
        ),
        relay_last_event_at=relay_last,
        relay_event_count=40,
        attributed_event_count=40,
        claims={"lane-dropped": NOW - dt.timedelta(minutes=40)},
        terminals={},
    )


async def _drive(
    monkeypatch: pytest.MonkeyPatch,
    read: gather_module.ModelLaneWindowRead,
    wait: float,
) -> dict[str, list[bytes]]:
    monkeypatch.setattr(
        gather_module.PostgresLaneWindowReader,
        "read_window",
        lambda *_args: read,
    )
    monkeypatch.setattr(alert_module, "_resolve_channel", lambda: "C0TEST")
    command_topic, evaluated_topic, slack_topic = _topics()
    manifest = discover_contracts_from_paths(CONTRACTS)
    assert not manifest.errors, manifest.errors

    bus = EventBusInmemory(environment="lane-liveness", group="lane-liveness")
    await bus.start()
    seen: dict[str, list[bytes]] = {
        command_topic: [],
        evaluated_topic: [],
        slack_topic: [],
    }
    alerted = asyncio.Event()

    def _collector(topic: str) -> Callable[[object], Awaitable[None]]:
        async def _collect(message: object) -> None:
            seen[topic].append(bytes(getattr(message, "value", b"")))
            if topic == slack_topic:
                alerted.set()

        return _collect

    for topic in seen:
        await bus.subscribe(
            topic, on_message=_collector(topic), group_id=f"probe-{topic}"
        )

    engine = MessageDispatchEngine()
    report = await wire_from_manifest(
        ModelAutoWiringManifest(contracts=tuple(manifest.contracts)),
        engine,
        event_bus=bus,
        environment="local",
    )
    assert report.total_failed == 0
    assert report.total_wired == 3
    engine.freeze()

    await bus.publish(TICK_TOPIC, None, _tick_wire(), None)
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(alerted.wait(), timeout=wait)
    await bus.close()
    return seen


async def test_tick_with_a_dropped_lane_reaches_the_slack_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = await _drive(
        monkeypatch, _read(relay_last=NOW - dt.timedelta(seconds=5)), wait=30
    )
    command_topic, evaluated_topic, slack_topic = _topics()

    assert len(seen[command_topic]) == 1, "the tick published no liveness command"
    assert len(seen[evaluated_topic]) == 1, "the compute node never evaluated it"
    assert len(seen[slack_topic]) == 1, "the dropped lane produced no alert"
    assert "lane-dropped" in json.dumps(json.loads(seen[slack_topic][0]))


async def test_a_silent_relay_evaluates_but_never_alerts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = await _drive(
        monkeypatch, _read(relay_last=NOW - dt.timedelta(minutes=40)), wait=3
    )
    _, evaluated_topic, slack_topic = _topics()

    assert len(seen[evaluated_topic]) == 1
    assert seen[slack_topic] == []
