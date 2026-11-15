# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains of the host drift orchestrator on the in-memory bus (OMN-24197).

The real contract.yaml is wired through ``wire_from_manifest`` onto an in-memory bus. A runtime tick opens the
schedule; the run event and the schedule run event leave on the contract's topics. The overlay is the node's
in-memory profile, found through ONEX_SKILL_OVERLAY_ROOTS.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import uuid
from collections.abc import Awaitable, Callable
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

from omnimarket.nodes.node_host_drift_orchestrator.handlers import (
    handler_host_drift_scheduled as scheduled_module,
)
from omnimarket.nodes.node_host_drift_orchestrator.handlers.sender_in_memory import (
    InMemoryHostDriftSender,
)

pytestmark = pytest.mark.unit

NODE = "node_host_drift_orchestrator"
NODE_DIR = (
    Path(__file__).resolve().parents[2].parent / "src" / "omnimarket" / "nodes" / NODE
)
CONTRACT = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
ROUTES = {r["operation"]: r["topic"] for r in CONTRACT["handler_routing"]["handlers"]}
TICK_TOPIC = ROUTES["host_drift.scheduled_run"]
RUN_TOPIC, SCHEDULE_TOPIC = CONTRACT["event_bus"]["publish_topics"]
OVERLAY = yaml.safe_load((NODE_DIR / "overlays" / "in_memory.yaml").read_text())
HOSTS = [row["name"] for row in OVERLAY["hosts"]]
NOW = dt.datetime(2026, 11, 14, 12, 0, tzinfo=dt.UTC)


@pytest.fixture(autouse=True)
def _overlay_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "overlays" / NODE
    root.mkdir(parents=True)
    (root / "overlay.yaml").write_text(yaml.safe_dump(OVERLAY))
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path / "overlays"))


def _tick() -> bytes:
    tick = ModelRuntimeTick(
        now=NOW,
        tick_id=uuid.uuid4(),
        sequence_number=1,
        scheduled_at=NOW,
        correlation_id=uuid.uuid4(),
        scheduler_id="omn24197-test",
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
    bus = EventBusInmemory(environment="omn24197", group="omn24197")
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
    assert set(CONTRACT["event_bus"]["subscribe_topics"]) == {TICK_TOPIC}


async def test_golden_chain_a_tick_publishes_one_completed_run_naming_each_host() -> (
    None
):
    seen = await _drive((TICK_TOPIC, _tick()))

    assert len(seen[RUN_TOPIC]) == 1
    run = seen[RUN_TOPIC][0]
    assert run["outcome"] == "completed"
    assert [v["host"] for v in run["verdicts"]] == HOSTS
    assert {v["verdict"] for v in run["verdicts"]} == {"in_sync"}
    assert [e["outcome"] for e in seen[SCHEDULE_TOPIC]] == ["completed"]
    assert seen[SCHEDULE_TOPIC][0]["job_id"] == OVERLAY["job_id"]


async def test_error_chain_a_silent_host_fails_the_run_and_the_schedule_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sender = InMemoryHostDriftSender(silent=frozenset({HOSTS[2]}))
    monkeypatch.setattr(scheduled_module, "build_sender", lambda _overlay: sender)

    seen = await _drive((TICK_TOPIC, _tick()))

    run = seen[RUN_TOPIC][0]
    assert run["outcome"] == "failed"
    assert run["failed_hosts"] == [HOSTS[2]]
    assert {v["host"]: v["verdict"] for v in run["verdicts"]} == {
        HOSTS[0]: "in_sync",
        HOSTS[1]: "in_sync",
        HOSTS[2]: "no_result",
        HOSTS[3]: "in_sync",
    }
    assert [e["outcome"] for e in seen[SCHEDULE_TOPIC]] == ["failed"]
    assert HOSTS[2] in seen[SCHEDULE_TOPIC][0]["detail"]


async def test_error_chain_two_deliveries_of_one_tick_send_four_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sender = InMemoryHostDriftSender()
    monkeypatch.setattr(scheduled_module, "build_sender", lambda _overlay: sender)
    tick = _tick()

    seen = await _drive((TICK_TOPIC, tick), (TICK_TOPIC, tick))

    assert len(sender.sent) == 4
    assert len(seen[RUN_TOPIC]) == 1
