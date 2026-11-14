# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains of the convergence orchestrator on the in-memory bus (OMN-24198).

The real contract.yaml is wired through ``wire_from_manifest`` onto an in-memory bus. A runtime tick opens the schedule; the run
event (one typed result per host) and the schedule run event leave on the contract's topics. The overlay is the node's in-memory
profile, found through ONEX_SKILL_OVERLAY_ROOTS; only the lab-work sender's answers are scripted.
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

from omnimarket.models.model_lab_work_unit import EnumLabWorkUnitStatus
from omnimarket.nodes.node_convergence_orchestrator.handlers import (
    handler_convergence_scheduled as scheduled_module,
)
from omnimarket.nodes.node_convergence_orchestrator.handlers.sender_in_memory import (
    InMemoryConvergenceSender,
)

pytestmark = pytest.mark.unit

NODE = "node_convergence_orchestrator"
NODE_DIR = (
    Path(__file__).resolve().parents[2].parent / "src" / "omnimarket" / "nodes" / NODE
)
CONTRACT = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
ROUTES = {r["operation"]: r["topic"] for r in CONTRACT["handler_routing"]["handlers"]}
TICK_TOPIC = ROUTES["convergence.scheduled_run"]
RUN_TOPIC, SCHEDULE_TOPIC = CONTRACT["event_bus"]["publish_topics"]
OVERLAY = yaml.safe_load((NODE_DIR / "overlays" / "in_memory.yaml").read_text())
HOSTS = [h["name"] for h in OVERLAY["hosts"]]
NOW = dt.datetime(2026, 11, 14, 4, 0, tzinfo=dt.UTC)


@pytest.fixture(autouse=True)
def _overlay_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    root = tmp_path / "overlays" / NODE
    root.mkdir(parents=True)
    (root / "overlay.yaml").write_text(yaml.safe_dump(OVERLAY))
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path / "overlays"))
    return


def _tick() -> bytes:
    tick = ModelRuntimeTick(
        now=NOW,
        tick_id=uuid.uuid4(),
        sequence_number=1,
        scheduled_at=NOW,
        correlation_id=uuid.uuid4(),
        scheduler_id="omn24198-test",
        tick_interval_ms=1000,
    )
    envelope: ModelEventEnvelope[object] = ModelEventEnvelope(
        payload=tick.model_dump(mode="json"),
        correlation_id=tick.correlation_id,
        envelope_timestamp=NOW,
        event_type="platform.runtime-tick",
    )
    return envelope.model_dump_json().encode("utf-8")


async def _drive(
    sender: InMemoryConvergenceSender, monkeypatch: pytest.MonkeyPatch
) -> dict[str, list[dict[str, Any]]]:
    monkeypatch.setattr(scheduled_module, "build_sender", lambda _overlay: sender)
    manifest = discover_contracts_from_paths([NODE_DIR / "contract.yaml"])
    assert not manifest.errors, manifest.errors
    bus = EventBusInmemory(environment="omn24198", group="omn24198")
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
    await bus.publish(TICK_TOPIC, None, _tick(), None)
    await asyncio.sleep(2.0)
    await bus.close()
    return seen


def test_the_contract_names_the_topics_the_chain_uses() -> None:
    assert CONTRACT["node_type"] == "orchestrator"
    assert CONTRACT["terminal_event"] == RUN_TOPIC
    assert CONTRACT["event_bus"]["subscribe_topics"] == [TICK_TOPIC]


async def test_golden_chain_a_tick_publishes_one_completed_run_with_a_result_per_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sender = InMemoryConvergenceSender(
        {
            h: (
                EnumLabWorkUnitStatus.COMPLETED,
                0,
                f"CLEAN         /reg/{h}/platform: on main at the upstream tip",
            )
            for h in HOSTS
        }
    )

    seen = await _drive(sender, monkeypatch)

    assert len(seen[RUN_TOPIC]) == 1
    run = seen[RUN_TOPIC][0]
    assert run["outcome"] == "completed"
    assert run["mode"] == "observe"
    assert sorted(h["host"] for h in run["hosts"]) == sorted(HOSTS)
    assert {h["outcome"] for h in run["hosts"]} == {"clean"}
    assert [e["outcome"] for e in seen[SCHEDULE_TOPIC]] == ["completed"]
    assert seen[SCHEDULE_TOPIC][0]["job_id"] == OVERLAY["job_id"]


async def test_error_chain_a_clone_the_host_cannot_converge_fails_the_run_and_names_the_clone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sender = InMemoryConvergenceSender(
        {
            HOSTS[1]: (
                EnumLabWorkUnitStatus.COMPLETED,
                1,
                "REFUSED       /reg/omnidash refused 400s: holds local work: modified 1 (a.ts) [alarm: failed: no channel]",
            )
        }
    )

    seen = await _drive(sender, monkeypatch)

    run = seen[RUN_TOPIC][0]
    assert run["outcome"] == "failed"
    refused = next(h for h in run["hosts"] if h["host"] == HOSTS[1])
    assert refused["outcome"] == "refused"
    assert [(c["clone"], c["status"]) for c in refused["clones"]] == [
        ("/reg/omnidash", "refused")
    ]
    assert "modified 1 (a.ts)" in refused["clones"][0]["reason"]
    assert [e["outcome"] for e in seen[SCHEDULE_TOPIC]] == ["failed"]
