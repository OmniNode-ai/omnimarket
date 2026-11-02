# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The three dispatching nodes decide the same on the in-memory bus and on a Redpanda bus (OMN-21958, AC4).

Each scenario's facts (the stops, the overlay's policy, now, the lane and the exemption) travel as a JSON message on
the bus under test; the node's consumer rebuilds the request, runs the node and publishes the decision as a JSON
message back. The decision projection of every scenario is one row of the run, and the parity run's own comparison
(``compare_runs``) judges the in-memory run against the Redpanda run.

The in-memory leg runs everywhere. The Redpanda leg runs against the broker the lab names in
``STOP_GATE_PARITY_BOOTSTRAP`` (a plaintext lab broker the caller started for the proof, never the dev lane's):

    STOP_GATE_PARITY_BOOTSTRAP=<host:port> uv run pytest -k stop_gate_parity -q -m kafka
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.models.stop_gate import EnumStopMode, ModelStopGateFacts
from omnimarket.nodes.node_binding_parity_orchestrator.handlers.handler_compare_runs import (
    compare_runs,
)
from omnimarket.nodes.node_binding_parity_orchestrator.models.model_parity import (
    ModelScenarioRow,
    ModelScenarioRun,
)
from omnimarket.nodes.node_binding_parity_orchestrator.overlay import (
    ModelFactoryBindingsOverlay,
)
from omnimarket.nodes.node_idle_slot_fill_effect import models as fill_models
from omnimarket.nodes.node_idle_slot_fill_effect.handlers import (
    handler_idle_slot_fill as fill_handler,
)
from omnimarket.nodes.node_landing_worker_engine_compute import (
    HandlerLandingWorkerEngine,
)
from omnimarket.nodes.node_landing_worker_engine_compute.models import (
    ModelLandingWorkerEngineRequest,
)
from omnimarket.nodes.node_remote_lane_compute.handlers.handler_remote_lane_placement import (
    HandlerRemoteLanePlacement,
)
from omnimarket.nodes.node_remote_lane_compute.models import (
    ModelRemoteLaneHostReading,
    ModelRemoteLanePlacementRequest,
)
from tests.nodes.node_andon_stop_reducer.stop_gate_facts import (
    CLASS,
    SWARM,
    facts,
    stop,
)
from tests.nodes.node_idle_slot_fill_effect.overlay_fixture import ENV as FILL_ENV

pytestmark = pytest.mark.unit

BOOTSTRAP_ENV = "STOP_GATE_PARITY_BOOTSTRAP"
NODES = ("fill", "placement", "worker_engine")
CLAUDE = "claude_opus"
CODEX = "codex_high"
TIMEOUT_S = 30.0
OVERLAY = ModelFactoryBindingsOverlay.model_validate(
    {
        "profiles": [{"id": "inmemory", "baseline": True}, {"id": "redpanda"}],
        "bindings": {"inmemory": {"BUS": "inmemory"}, "redpanda": {"BUS": "redpanda"}},
        "proof_bindings": ["inmemory", "redpanda"],
    }
)


@pytest.fixture(autouse=True)
def _fill_overlay(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fill reads its own ``idle_slot_fill`` and ``repos`` overlays; the stop policy travels in the facts."""
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", FILL_ENV["ONEX_SKILL_OVERLAY_ROOTS"])


# ---------------------------------------------------------------- the nodes, as a consumer runs them


def _placement_api(claude: Any, codex: Any) -> Mock:
    api = Mock()
    api.CODEX_ENGINE = CODEX
    api.choose.return_value = claude
    api.choose_codex.return_value = codex
    api.resolve_engine.side_effect = lambda engine: engine
    api.preflight_skips.return_value = {}
    return api


def run_fill(gate: ModelStopGateFacts) -> dict[str, Any]:
    result = fill_handler.HandlerIdleSlotFill().handle(
        fill_models.ModelIdleSlotFillRequest(
            now="2026-11-02T03:00:00Z",
            hosts=[
                {
                    "name": "host-a",
                    "local": False,
                    "admission": {"claude_slots": 2, "codex_slots": 0},
                }
            ],
            lines=["MISSING lab-headroom:host-a | free Claude slots 3 | FIX: dispatch"],
            backlog=[
                {
                    "kind": "ticket",
                    "key": "t-1",
                    "repo": "omnimarket",
                    "ticket": "OMN-1",
                }
            ],
            standing_tickets={"ticket": "OMN-9101"},
            stop_gate=gate,
        )
    )
    return {
        "sends": [[s["key"], s["model"]] for s in result.sends],
        "skipped": result.skipped,
        "observed": result.stop_observed,
    }


def run_placement(gate: ModelStopGateFacts) -> dict[str, Any]:
    result = HandlerRemoteLanePlacement().handle(
        ModelRemoteLanePlacementRequest(
            engine=CLAUDE,
            readings=(
                ModelRemoteLaneHostReading(
                    name="host-a",
                    engines=(CLAUDE,),
                    lane_slots=2,
                    lane_cap=4,
                    rank_free=5.0,
                ),
            ),
            escalation_reason="complexity:B4",
            stop_gate=gate,
        )
    )
    return {
        "host": result.host,
        "outcome": result.outcome.value,
        "refusal": result.refusal,
        "gate": None if result.stop_gate is None else result.stop_gate.line,
    }


def run_worker_engine(gate: ModelStopGateFacts) -> dict[str, Any]:
    claude = SimpleNamespace(engine=CLAUDE, host=SimpleNamespace(name="host-a"))
    codex = SimpleNamespace(engine=CODEX, host=SimpleNamespace(name="host-b"))
    result = HandlerLandingWorkerEngine().handle(
        ModelLandingWorkerEngineRequest(
            readings=(),
            requested_engine=CLAUDE,
            codex_first=False,
            cause=False,
            codex_allowed=frozenset(),
            env={},
            placement=_placement_api(claude, codex),
            stop_gate=gate,
        )
    )
    return {
        "engine": None if result.pick is None else result.pick.engine,
        "record": result.record,
    }


RUNNERS: dict[str, Callable[[ModelStopGateFacts], dict[str, Any]]] = {
    "fill": run_fill,
    "placement": run_placement,
    "worker_engine": run_worker_engine,
}

# ---------------------------------------------------------------- the scenarios, as facts


def scenarios(root: Path) -> dict[str, ModelStopGateFacts]:
    def stopped(key: str) -> Any:
        return stop(key=key)

    out: dict[str, ModelStopGateFacts] = {}
    for node in NODES:
        key = "opus" if node != "fill" else "claude_sonnet"
        out[f"{node}:enforce"] = facts(root / node / "e", "enforce", stopped(key))
        out[f"{node}:observe"] = facts(root / node / "o", "observe", stopped(key))
        out[f"{node}:swarm"] = facts(
            root / node / "s", "enforce", stopped(key), lane_id=SWARM
        )
        out[f"{node}:no-stop"] = facts(root / node / "n", "enforce")
        out[f"{node}:mode-missing"] = facts(
            root / node / "m", "enforce", stopped(key)
        ).model_copy(
            update={
                "policy": facts(root / node / "x", "enforce").policy.model_copy(
                    update={"modes": {}}
                )
            }
        )
    return out


# ---------------------------------------------------------------- the buses


class Bus:
    """Carry one JSON request to a node's consumer and the JSON decision back."""

    async def start(self) -> None: ...

    async def call(self, node: str, payload: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def stop(self) -> None: ...


def decide(node: str, payload: dict[str, Any]) -> dict[str, Any]:
    gate = ModelStopGateFacts.model_validate(payload["facts"])
    return {"id": payload["id"], "decision": RUNNERS[node](gate)}


class InmemoryBus(Bus):
    def __init__(self) -> None:
        self.bus = EventBusInmemory(environment="stop-gate-parity", group="parity")
        self.waiting: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self.prefix = uuid.uuid4().hex

    async def start(self) -> None:
        await self.bus.start()
        for node in NODES:

            async def serve(message: Any, node: str = node) -> None:
                reply = decide(node, json.loads(message.value))
                await self.bus.publish(
                    f"{self.prefix}.res", None, json.dumps(reply).encode()
                )

            await self.bus.subscribe(
                f"{self.prefix}.{node}.req", on_message=serve, group_id=f"serve-{node}"
            )

        async def answer(message: Any) -> None:
            reply = json.loads(message.value)
            self.waiting.pop(reply["id"]).set_result(reply)

        await self.bus.subscribe(
            f"{self.prefix}.res", on_message=answer, group_id="ask"
        )

    async def call(self, node: str, payload: dict[str, Any]) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self.waiting[payload["id"]] = future
        await self.bus.publish(
            f"{self.prefix}.{node}.req", None, json.dumps(payload).encode()
        )
        return await asyncio.wait_for(future, TIMEOUT_S)

    async def stop(self) -> None:
        await self.bus.close()


class RedpandaBus(Bus):
    def __init__(self, bootstrap: str) -> None:
        self.bootstrap = bootstrap
        self.prefix = f"stop-gate-parity-{uuid.uuid4().hex}"
        self.tasks: list[asyncio.Task[None]] = []
        self.waiting: dict[str, asyncio.Future[dict[str, Any]]] = {}

    async def start(self) -> None:
        from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

        self.producer = AIOKafkaProducer(bootstrap_servers=self.bootstrap)
        await self.producer.start()
        self.consumers: list[Any] = []

        async def consume(topic: str, handle: Callable[[dict[str, Any]], Any]) -> None:
            consumer = AIOKafkaConsumer(
                topic,
                bootstrap_servers=self.bootstrap,
                group_id=f"{self.prefix}-{topic}",
                auto_offset_reset="earliest",
            )
            await consumer.start()
            self.consumers.append(consumer)
            async for message in consumer:
                await handle(json.loads(message.value))

        for node in NODES:

            async def serve(request: dict[str, Any], node: str = node) -> None:
                reply = decide(node, request)
                await self.producer.send_and_wait(
                    f"{self.prefix}.res", json.dumps(reply).encode()
                )

            self.tasks.append(
                asyncio.create_task(consume(f"{self.prefix}.{node}.req", serve))
            )

        async def answer(reply: dict[str, Any]) -> None:
            future = self.waiting.pop(reply["id"], None)
            if future is not None:
                future.set_result(reply)

        self.tasks.append(asyncio.create_task(consume(f"{self.prefix}.res", answer)))
        await asyncio.sleep(3.0)  # let the consumers join their topics

    async def call(self, node: str, payload: dict[str, Any]) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self.waiting[payload["id"]] = future
        await self.producer.send_and_wait(
            f"{self.prefix}.{node}.req", json.dumps(payload).encode()
        )
        return await asyncio.wait_for(future, TIMEOUT_S)

    async def stop(self) -> None:
        for task in self.tasks:
            task.cancel()
        for consumer in self.consumers:
            await consumer.stop()
        await self.producer.stop()


# ---------------------------------------------------------------- the run


async def run_over(
    bus: Bus, root: Path, profile: str, binding: str
) -> ModelScenarioRun:
    await bus.start()
    try:
        rows = []
        for name, gate in scenarios(root).items():
            node = name.split(":")[0]
            payload = {"id": uuid.uuid4().hex, "facts": gate.model_dump(mode="json")}
            reply = await bus.call(node, payload)
            rows.append(
                ModelScenarioRow(
                    key=name, content=json.dumps(reply["decision"], sort_keys=True)
                )
            )
    finally:
        await bus.stop()
    return ModelScenarioRun(
        profile_id=profile,
        binding=binding,
        proof_eligible=True,
        verdicts=("PASS",),
        rows=tuple(rows),
        counts={"decisions": len(rows)},
    )


def in_process(root: Path) -> dict[str, str]:
    return {
        name: json.dumps(RUNNERS[name.split(":")[0]](gate), sort_keys=True)
        for name, gate in scenarios(root).items()
    }


def make_bus(binding: str) -> Bus:
    if binding == "inmemory":
        return InmemoryBus()
    return RedpandaBus(os.environ[BOOTSTRAP_ENV])


def parity_run(root: Path, *, profiles: tuple[str, ...]) -> Any:
    chosen = ModelFactoryBindingsOverlay.model_validate(
        {
            "profiles": [p.model_dump() for p in OVERLAY.profiles if p.id in profiles],
            "bindings": {p: OVERLAY.bindings[p] for p in profiles},
            "proof_bindings": list(OVERLAY.proof_bindings),
        }
    )
    runs = tuple(
        asyncio.run(
            run_over(
                make_bus(chosen.binding_id(profile.id, "BUS")),
                root / profile.id,
                profile.id,
                chosen.binding_id(profile.id, "BUS"),
            )
        )
        for profile in chosen.profiles
    )
    return runs


# ---------------------------------------------------------------- the tests


def test_stop_gate_parity_the_decisions_of_the_three_nodes_are_what_the_gate_says(
    tmp_path: Path,
) -> None:
    decisions = {k: json.loads(v) for k, v in in_process(tmp_path).items()}
    assert decisions["placement:enforce"]["outcome"] == "STOPPED"
    assert decisions["placement:observe"]["outcome"] == "ADMITTED"
    assert decisions["placement:swarm"]["host"] == "host-a"
    assert decisions["fill:enforce"]["sends"] == []
    assert decisions["fill:observe"]["sends"] != []
    assert decisions["worker_engine:enforce"]["record"]["engine_refused"]
    assert f"andon_stop.modes.{CLASS}" in json.dumps(decisions["fill:mode-missing"])
    assert f"andon_stop.modes.{CLASS}" in json.dumps(
        decisions["placement:mode-missing"]
    )
    assert EnumStopMode.ENFORCE.value not in json.dumps(decisions["placement:observe"])


def test_stop_gate_parity_in_memory_bus_matches_the_in_process_decisions(
    tmp_path: Path,
) -> None:
    [run] = parity_run(tmp_path / "bus", profiles=("inmemory",))
    assert {row.key: row.content for row in run.rows} == in_process(tmp_path / "direct")


@pytest.mark.kafka
@pytest.mark.integration
def test_stop_gate_parity_redpanda_overlay_matches_the_in_memory_overlay(
    tmp_path: Path,
) -> None:
    if not os.environ.get(BOOTSTRAP_ENV):
        pytest.skip(f"Kafka not reachable: {BOOTSTRAP_ENV} names no lab broker")
    runs = parity_run(tmp_path, profiles=("inmemory", "redpanda"))
    result = compare_runs(runs)
    assert result.passed, result.first_difference
    assert {row.key: row.content for row in runs[1].rows} == in_process(
        tmp_path / "direct"
    )
    assert len(runs[1].rows) == len(NODES) * 5
