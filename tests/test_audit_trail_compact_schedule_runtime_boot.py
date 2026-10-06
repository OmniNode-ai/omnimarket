# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Runtime boot: a runtime tick in the daily slot reaches the audit trail compactor.

Loads the real contracts from disk, boots them through ``wire_from_manifest``
(the call the kernel makes), publishes a ``ModelRuntimeTick`` envelope on the
tick topic, and reads the compactor's command and compacted event off the bus.
Only the bus is in-memory; the contracts, handlers, dispatch engine and
dispatch-result applier are the runtime's own.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
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

from omnimarket.nodes.node_audit_trail_compact_schedule_compute.handlers.handler_audit_trail_compact_schedule import (
    schedule_config,
)

pytestmark = pytest.mark.unit

NODES = Path(__file__).parents[1] / "src" / "omnimarket" / "nodes"
SCHEDULE = NODES / "node_audit_trail_compact_schedule_compute" / "contract.yaml"
COMPACTOR = NODES / "node_audit_trail_compactor" / "contract.yaml"
TICK_TOPIC = "onex.intent.platform.runtime-tick.v1"


def _topics() -> tuple[str, str]:
    contract = yaml.safe_load(COMPACTOR.read_text())
    return (
        contract["runtime_dispatch"]["command_topic"],
        contract["terminal_event"],
    )


def _tick_wire(now: dt.datetime) -> bytes:
    tick = ModelRuntimeTick(
        now=now,
        tick_id=uuid4(),
        sequence_number=1,
        scheduled_at=now,
        correlation_id=uuid4(),
        scheduler_id="runtime-boot-test",
        tick_interval_ms=1000,
    )
    envelope: ModelEventEnvelope[object] = ModelEventEnvelope(
        payload=tick.model_dump(mode="json"),
        correlation_id=tick.correlation_id,
        envelope_timestamp=now,
    )
    return envelope.model_dump_json().encode("utf-8")


async def _drive(now: dt.datetime, *, wait_seconds: float) -> dict[str, list[bytes]]:
    command_topic, compacted_topic = _topics()
    manifest = discover_contracts_from_paths([SCHEDULE, COMPACTOR])
    assert not manifest.errors, manifest.errors
    assert {c.name for c in manifest.contracts} == {
        "node_audit_trail_compact_schedule_compute",
        "node_audit_trail_compactor",
    }

    bus = EventBusInmemory(environment="audit-compact", group="audit-compact")
    await bus.start()
    seen: dict[str, list[bytes]] = {command_topic: [], compacted_topic: []}
    compacted = asyncio.Event()

    def _collector(topic: str) -> Callable[[object], Awaitable[None]]:
        async def _collect(message: object) -> None:
            seen[topic].append(bytes(getattr(message, "value", b"")))
            if topic == compacted_topic:
                compacted.set()

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
    assert report.total_wired == 2
    engine.freeze()

    await bus.publish(TICK_TOPIC, None, _tick_wire(now), None)
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(compacted.wait(), timeout=wait_seconds)
    await bus.close()
    return seen


async def test_slot_tick_runs_the_compactor_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = schedule_config()
    slot = dt.datetime(2026, 10, 5, cfg.run_hour_utc, cfg.run_minute_utc, tzinfo=dt.UTC)
    command_topic, compacted_topic = _topics()

    seen = await _drive(slot, wait_seconds=30)

    assert len(seen[command_topic]) == 1, "the slot tick published no compactor command"
    assert len(seen[compacted_topic]) == 1, "the compactor never ran on that command"


async def test_off_slot_tick_publishes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = schedule_config()
    off = dt.datetime(
        2026, 10, 5, (cfg.run_hour_utc + 1) % 24, cfg.run_minute_utc, tzinfo=dt.UTC
    )
    command_topic, compacted_topic = _topics()

    seen = await _drive(off, wait_seconds=2)

    assert seen[command_topic] == []
    assert seen[compacted_topic] == []
