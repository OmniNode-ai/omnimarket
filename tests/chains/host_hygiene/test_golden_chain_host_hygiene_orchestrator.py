# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains of the host hygiene orchestrator on the in-memory bus (OMN-23707).

The real contract.yaml is wired through ``wire_from_manifest`` onto an in-memory bus. Host disk readings arrive on the watermark
node's read topics in the wire shape, then a runtime tick opens the schedule; the run event and the schedule run event leave on
the contract's topics. The overlay is the node's in-memory profile, found through ONEX_SKILL_OVERLAY_ROOTS.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import uuid
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
from omnibase_infra.runtime.auto_wiring.models import ModelAutoWiringManifest
from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.models.model_host_disk_reading import (
    ModelDiskClassReading,
    ModelHostDiskRead,
    ModelHostDiskReadFailed,
    ModelHostDiskVolume,
)
from omnimarket.models.model_lab_work_unit import EnumLabWorkUnitStatus
from omnimarket.nodes.node_host_hygiene_orchestrator.handlers import (
    HOST_DISK_READINGS,
)
from omnimarket.nodes.node_host_hygiene_orchestrator.handlers import (
    handler_host_hygiene_scheduled as scheduled_module,
)
from omnimarket.nodes.node_host_hygiene_orchestrator.handlers.sender_in_memory import (
    InMemoryHostHygieneSender,
)

pytestmark = pytest.mark.unit

NODE = "node_host_hygiene_orchestrator"
NODE_DIR = (
    Path(__file__).resolve().parents[2].parent / "src" / "omnimarket" / "nodes" / NODE
)
CONTRACT = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
ROUTES = {r["operation"]: r["topic"] for r in CONTRACT["handler_routing"]["handlers"]}
READ_TOPIC = ROUTES["record_host_disk_read"]
FAILED_TOPIC = ROUTES["record_host_disk_read_failed"]
TICK_TOPIC = ROUTES["host_hygiene.scheduled_run"]
RUN_TOPIC, SCHEDULE_TOPIC = CONTRACT["event_bus"]["publish_topics"]
OVERLAY = yaml.safe_load((NODE_DIR / "overlays" / "in_memory.yaml").read_text())
HOST = OVERLAY["hosts"][0]["name"]
VOLUME = OVERLAY["hosts"][0]["pressure_volume"]
NOW = dt.datetime(2026, 11, 13, 12, 0, tzinfo=dt.UTC)


@pytest.fixture(autouse=True)
def _overlay_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    root = tmp_path / "overlays" / NODE
    root.mkdir(parents=True)
    (root / "overlay.yaml").write_text(yaml.safe_dump(OVERLAY))
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path / "overlays"))
    HOST_DISK_READINGS._entries.clear()
    yield
    HOST_DISK_READINGS._entries.clear()


def _wire(event: object, event_type: str) -> bytes:
    envelope: ModelEventEnvelope[object] = ModelEventEnvelope(
        payload=event, correlation_id=uuid.uuid4(), event_type=event_type
    )
    return json.dumps(envelope.model_dump(mode="json")).encode("utf-8")


def _read(used: int) -> bytes:
    return _wire(
        ModelHostDiskRead(
            correlation_id="c1",
            host_name=HOST,
            read_at=NOW - dt.timedelta(seconds=30),
            volumes=(
                ModelHostDiskVolume(
                    path=VOLUME, total_bytes=100, free_bytes=100 - used, used_bytes=used
                ),
            ),
            classes=(
                ModelDiskClassReading(
                    class_name="uv_cache", status="unread", unread_reason="not read"
                ),
            ),
        ).model_dump(mode="json"),
        "omnimarket.host-disk-read",
    )


def _failed() -> bytes:
    return _wire(
        ModelHostDiskReadFailed(
            correlation_id="c2",
            host_name=HOST,
            read_at=NOW - dt.timedelta(seconds=10),
            reason="volume_unreadable",
            detail="the volume could not be read",
        ).model_dump(mode="json"),
        "omnimarket.host-disk-read-failed",
    )


def _tick() -> bytes:
    tick = ModelRuntimeTick(
        now=NOW,
        tick_id=uuid.uuid4(),
        sequence_number=1,
        scheduled_at=NOW,
        correlation_id=uuid.uuid4(),
        scheduler_id="omn23707-test",
        tick_interval_ms=1000,
    )
    envelope: ModelEventEnvelope[object] = ModelEventEnvelope(
        payload=tick.model_dump(mode="json"),
        correlation_id=tick.correlation_id,
        envelope_timestamp=NOW,
        event_type="platform.runtime-tick",
    )
    return envelope.model_dump_json().encode("utf-8")


async def _drive(*inputs: tuple[str, bytes]) -> dict[str, list[dict[str, Any]]]:
    manifest = discover_contracts_from_paths([NODE_DIR / "contract.yaml"])
    assert not manifest.errors, manifest.errors
    bus = EventBusInmemory(environment="omn23707", group="omn23707")
    await bus.start()
    seen: dict[str, list[dict[str, Any]]] = {RUN_TOPIC: [], SCHEDULE_TOPIC: []}

    def _collector(topic: str) -> Callable[[object], Awaitable[None]]:
        async def _collect(message: object) -> None:
            body: dict[str, Any] = json.loads(bytes(getattr(message, "value", b"")))
            seen[topic].append(body.get("payload", body))

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
    engine.freeze()
    for topic, wire in inputs:
        await bus.publish(topic, None, wire, None)
        await asyncio.sleep(0.5)
    await asyncio.sleep(1.5)
    await bus.close()
    return seen


def test_the_contract_names_the_topics_the_chain_uses() -> None:
    assert CONTRACT["node_type"] == "orchestrator"
    assert CONTRACT["terminal_event"] == RUN_TOPIC
    assert set(CONTRACT["event_bus"]["subscribe_topics"]) == {
        READ_TOPIC,
        FAILED_TOPIC,
        TICK_TOPIC,
    }


async def test_golden_chain_reading_then_tick_publishes_one_completed_run() -> None:
    seen = await _drive((READ_TOPIC, _read(90)), (TICK_TOPIC, _tick()))

    assert len(seen[RUN_TOPIC]) == 1
    run = seen[RUN_TOPIC][0]
    assert run["outcome"] == "completed"
    assert {u["outcome"] for u in run["units"]} == {"completed"}
    assert {d["reason"] for d in run["decided"]} == {"disk_pressure"}
    assert [e["outcome"] for e in seen[SCHEDULE_TOPIC]] == ["completed"]
    assert seen[SCHEDULE_TOPIC][0]["job_id"] == OVERLAY["job_id"]


async def test_error_chain_a_failed_reading_then_tick_applies_the_unread_policy() -> (
    None
):
    seen = await _drive((FAILED_TOPIC, _failed()), (TICK_TOPIC, _tick()))

    run = seen[RUN_TOPIC][0]
    reasons = {d["cleanup_id"]: d["reason"] for d in run["decided"]}
    assert reasons == {
        "docker_hygiene": "unread_run_anyway",
        "file_retention": "unread_skipped",
    }
    assert [u["cleanup_id"] for u in run["units"]] == ["docker_hygiene"]


async def test_error_chain_a_failing_cleanup_fails_the_run_and_the_schedule_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sender = InMemoryHostHygieneSender(
        {HOST: (EnumLabWorkUnitStatus.COMPLETED, 2, "exit 2")}
    )
    monkeypatch.setattr(scheduled_module, "build_sender", lambda _overlay: sender)

    seen = await _drive((READ_TOPIC, _read(90)), (TICK_TOPIC, _tick()))

    run = seen[RUN_TOPIC][0]
    assert run["outcome"] == "failed"
    assert {u["outcome"] for u in run["units"]} == {"failed"}
    assert [e["outcome"] for e in seen[SCHEDULE_TOPIC]] == ["failed"]
