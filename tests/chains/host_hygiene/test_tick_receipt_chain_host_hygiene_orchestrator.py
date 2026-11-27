# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab_work tick chain through the runtime's own auto-wiring: the tick publishes its units and returns, the receipts settle the run.

The contract is wired the way the runtime wires it (contract discovery, handler wiring, a message dispatch engine over an
in-memory bus). A scripted pool host stands in for the lab work serve: it reads the command topic, holds each unit for a
while (the stand-in for a cleanup that runs for hours), then answers on the completed terminal. The run event must not
exist while the hosts are silent, and must exist once the last host has answered. Before the change the tick held the
dispatch for the whole wait.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import time
import uuid
from collections.abc import Iterator
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
    ModelHostDiskVolume,
)
from omnimarket.nodes.node_host_hygiene_orchestrator.handlers import (
    HOST_DISK_READINGS,
)
from omnimarket.nodes.node_host_hygiene_orchestrator.handlers.contract_config import (
    lab_work_command_topic,
)
from omnimarket.nodes.node_host_hygiene_orchestrator.handlers.pending_runs import (
    PENDING_HOST_HYGIENE_RUNS,
)

pytestmark = pytest.mark.unit

NODE = "node_host_hygiene_orchestrator"
NODE_DIR = Path(__file__).resolve().parents[3] / "src" / "omnimarket" / "nodes" / NODE
CONTRACT = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
ROUTES = {r["operation"]: r["topic"] for r in CONTRACT["handler_routing"]["handlers"]}
READ_TOPIC = ROUTES["record_host_disk_read"]
TICK_TOPIC = ROUTES["host_hygiene.scheduled_run"]
COMPLETED_TOPIC = ROUTES["host_hygiene.unit_completed"]
RUN_TOPIC = next(
    t for t in CONTRACT["event_bus"]["publish_topics"] if "run-completed" in t
)
OVERLAY = yaml.safe_load(next(NODE_DIR.glob("*/lab_work.example.yaml")).read_text())
HOST = OVERLAY["hosts"][0]["name"]
VOLUME = OVERLAY["hosts"][0]["pressure_volume"]
NOW = dt.datetime(2026, 11, 25, 1, 9, 10, tzinfo=dt.UTC)
#: How long the scripted host holds each unit; the real wait was 8100 s.
HOST_HOLD_SECONDS = 3.0


@pytest.fixture(autouse=True)
def _overlay_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    root = tmp_path / "ov" / NODE
    root.mkdir(parents=True)
    (root / "overlay.yaml").write_text(yaml.safe_dump(OVERLAY))
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path / "ov"))
    HOST_DISK_READINGS._entries.clear()
    PENDING_HOST_HYGIENE_RUNS.clear()
    yield
    HOST_DISK_READINGS._entries.clear()
    PENDING_HOST_HYGIENE_RUNS.clear()


def _wire(payload: object, event_type: str, **extra: Any) -> bytes:
    envelope: ModelEventEnvelope[object] = ModelEventEnvelope(
        payload=payload, correlation_id=uuid.uuid4(), event_type=event_type, **extra
    )
    return json.dumps(envelope.model_dump(mode="json")).encode("utf-8")


def _read() -> bytes:
    return _wire(
        ModelHostDiskRead(
            correlation_id="c1",
            host_name=HOST,
            read_at=NOW - dt.timedelta(seconds=30),
            volumes=(
                ModelHostDiskVolume(
                    path=VOLUME, total_bytes=100, free_bytes=10, used_bytes=90
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


def _tick() -> bytes:
    tick = ModelRuntimeTick(
        now=NOW,
        tick_id=uuid.uuid4(),
        sequence_number=1,
        scheduled_at=NOW,
        correlation_id=uuid.uuid4(),
        scheduler_id="omn26316-test",
        tick_interval_ms=1000,
    )
    return (
        ModelEventEnvelope(
            payload=tick.model_dump(mode="json"),
            correlation_id=tick.correlation_id,
            envelope_timestamp=NOW,
            event_type="platform.runtime-tick",
        )
        .model_dump_json()
        .encode("utf-8")
    )


async def test_the_tick_publishes_its_units_the_run_waits_for_the_hosts_and_settles_on_the_last_receipt() -> (
    None
):
    manifest = discover_contracts_from_paths([NODE_DIR / "contract.yaml"])
    assert not manifest.errors, manifest.errors
    bus = EventBusInmemory(environment="omn26316", group="omn26316")
    await bus.start()
    stamps: dict[str, list[float]] = {"command": [], "run": []}
    runs: list[dict[str, Any]] = []
    answered: list[asyncio.Task[None]] = []

    async def _host(message: object) -> None:
        body = json.loads(bytes(getattr(message, "value", b"")))
        unit = body["payload"]
        stamps["command"].append(time.monotonic())

        async def _answer() -> None:
            await asyncio.sleep(HOST_HOLD_SECONDS)
            receipt = {
                "work_unit_id": unit["work_unit_id"],
                "target_host": unit["target_host"],
                "host": unit["target_host"],
                "repo": unit["repo"],
                "commit_sha": unit["commit_sha"],
                "lane": unit["lane"],
                "kind": unit["kind"],
                "status": "completed",
                "exit_code": 0,
                "log_path": "scripted",
                "output_tail": "ok",
                "duration_seconds": HOST_HOLD_SECONDS,
            }
            await bus.publish(
                COMPLETED_TOPIC,
                None,
                _wire(receipt, COMPLETED_TOPIC, parent_envelope_id=body["envelope_id"]),
                None,
            )

        answered.append(asyncio.create_task(_answer()))

    async def _run(message: object) -> None:
        stamps["run"].append(time.monotonic())
        runs.append(json.loads(bytes(getattr(message, "value", b"")))["payload"])

    await bus.subscribe(
        lab_work_command_topic(), on_message=_host, group_id="scripted-host"
    )
    await bus.subscribe(RUN_TOPIC, on_message=_run, group_id="probe-run")
    engine = MessageDispatchEngine()
    report = await wire_from_manifest(
        ModelAutoWiringManifest(contracts=tuple(manifest.contracts)),
        engine,
        event_bus=bus,
        environment="local",
    )
    assert report.total_failed == 0
    engine.freeze()

    await bus.publish(READ_TOPIC, None, _read(), None)
    await asyncio.sleep(0.3)
    started = time.monotonic()
    await bus.publish(TICK_TOPIC, None, _tick(), None)
    await asyncio.sleep(1.0)

    # The units are out on the command topic while every host is still silent, and no run event exists yet.
    assert len(stamps["command"]) == 2
    assert max(stamps["command"]) - started < 1.0
    assert runs == []

    await asyncio.gather(*answered)
    await asyncio.sleep(1.0)
    await bus.close()

    assert len(runs) == 1
    assert runs[0]["outcome"] == "completed"
    assert {u["outcome"] for u in runs[0]["units"]} == {"completed"}
    assert stamps["run"][0] - min(stamps["command"]) >= HOST_HOLD_SECONDS
