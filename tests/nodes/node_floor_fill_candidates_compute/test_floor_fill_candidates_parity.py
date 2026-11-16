# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_floor_fill_candidates_compute decides the same on the in-memory bus and on a Redpanda bus (G-NODE point 5).

Each scenario's request travels as a JSON message on the bus under test; the node's consumer rebuilds the request,
runs the node and publishes the decision as a JSON message back. The decision projection of every scenario is one row
of the run, and the parity run's own comparison (``compare_runs``) judges the in-memory run against the Redpanda run.

The in-memory leg runs everywhere. The Redpanda leg runs against the broker the lab names in
``FLOOR_FILL_PARITY_BOOTSTRAP`` (a plaintext lab broker the caller started for the proof, never the dev lane's):

    FLOOR_FILL_PARITY_BOOTSTRAP=<host:port> uv run pytest -m "floor_fill_parity and kafka" -q
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.models.floor_fill import (
    ModelFloorFillCandidatesRequest,
    ModelFloorStartCriterion,
)
from omnimarket.models.model_factory_floor_input import ModelFloorLinearIssue
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
from omnimarket.nodes.node_factory_floor_compute.handlers.handler_factory_floor import (
    compute_factory_floor,
)
from omnimarket.nodes.node_floor_fill_candidates_compute.handlers.handler_floor_fill_candidates import (
    HandlerFloorFillCandidates,
)
from omnimarket.nodes.node_floor_fill_candidates_compute.tests.support import (
    LINE,
    NOW,
    READY_10_40,
    all_ready_payloads_ok,
    all_ready_tickets_usable,
    fixture_text,
    item,
    minutes_before,
    record,
    request,
    reserved,
    usable,
)
from tests.nodes.node_factory_floor_compute.test_stopped_lane_child_tickets import (
    CHILD,
    ROOT,
    floor_request,
)

PIECES_FIXTURE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_floor_fill_candidates_compute/tests/fixtures"
    / "floor-simple-2026-11-07T1232Z.json"
)
pytestmark = [pytest.mark.unit, pytest.mark.floor_fill_parity]

BOOTSTRAP_ENV = "FLOOR_FILL_PARITY_BOOTSTRAP"
TIMEOUT_S = 30.0
OVERLAY = ModelFactoryBindingsOverlay.model_validate(
    {
        "profiles": [{"id": "inmemory", "baseline": True}, {"id": "redpanda"}],
        "bindings": {"inmemory": {"BUS": "inmemory"}, "redpanda": {"BUS": "redpanda"}},
        "proof_bindings": ["inmemory", "redpanda"],
    }
)


STOPPED_NOW = "2026-11-10T02:05:00Z"
STOPPED_LANE = "labrunpool-1050"


def stopped_record(*, children: bool) -> str:
    """The floor record of the 2026-11-09 ledger: the child-ticket lane is listed only when the snapshot links it."""
    linear = (ModelFloorLinearIssue(id=ROOT, children=(CHILD,)),) if children else ()
    return compute_factory_floor(floor_request(linear)).model_dump_json()


def stopped_request(*, children: bool) -> ModelFloorFillCandidatesRequest:
    return request(now=STOPPED_NOW, floor_record=stopped_record(children=children))


def criterion_scenarios() -> dict[str, ModelFloorFillCandidatesRequest]:
    """The start criterion on the bus: a met one (golden), an unmet one and an unparseable one (error)."""
    pieces = PIECES_FIXTURE.read_text()
    now = "2026-11-07T12:35:00Z"
    tasks = {
        "criterion:met": "Start when: piece 1 reads WORKING and omnimarket#4922 is merged",
        "criterion:unmet": "Start when: piece 2 reads WORKING",
        "criterion:unparseable": "Start when: the operator says go",
    }
    return {
        name: request(
            now=now,
            floor_record=record([item("T1")], generated_at="2026-11-07T12:34:00Z"),
            pieces_record=pieces,
            merged_refs=("omnimarket#4922",),
            start_criteria=(
                ModelFloorStartCriterion(
                    line_id=LINE, task_id="T1", start_when=start_when
                ),
            ),
            ticket_verdicts=(usable(LINE, "T1", "TCK-1"),),
        )
        for name, start_when in tasks.items()
    }


def integrating_scenarios() -> dict[str, ModelFloorFillCandidatesRequest]:
    """A record whose items are integrating (merged, being shown running) reads and does not unread the stopped lane."""
    document = json.loads(
        record(
            [item("T1", "integrating"), item("T2")], generated_at="2026-11-02T10:40:00Z"
        )
    )
    document["floor"]["stopped"] = [
        {
            "lane": "lane-a",
            "ticket": "TCK-9",
            "outcome": "partial",
            "branch": "branch-a",
            "rows": ["TERMINAL outcome=partial"],
        }
    ]
    return {
        "integrating:item": request(
            floor_record=json.dumps(document),
            ticket_verdicts=(usable(LINE, "T2", "TCK-2"),),
        ),
        "integrating:unknown-state": request(
            floor_record=record([item("T1", "unheard-of")])
        ),
    }


def scenarios() -> dict[str, ModelFloorFillCandidatesRequest]:
    """The golden scenario (the 10:40Z record) under each ticket input, plus the unread and the held cases."""
    first_line = next(iter(READY_10_40))
    return {
        "fixture:usable": request(ticket_verdicts=all_ready_tickets_usable()),
        "fixture:needs-ticket": request(payload_verdicts=all_ready_payloads_ok()),
        "fixture:held": request(
            ticket_verdicts=all_ready_tickets_usable(),
            max_fill_lanes_per_line=1,
            entries=(reserved(first_line, READY_10_40[first_line][0]),),
        ),
        "fixture:stale": request(
            now=minutes_before(NOW, -120), ticket_verdicts=all_ready_tickets_usable()
        ),
        "fixture:absent": request(floor_record=None),
        "stopped:child-ticket-lane": stopped_request(children=True),
        "stopped:no-snapshot-children": stopped_request(children=False),
        **criterion_scenarios(),
        **integrating_scenarios(),
    }


def decide(payload: dict[str, Any]) -> dict[str, Any]:
    result = HandlerFloorFillCandidates().handle(
        ModelFloorFillCandidatesRequest.model_validate(payload["request"])
    )
    return {"id": payload["id"], "decision": result.model_dump(mode="json")}


def in_process() -> dict[str, str]:
    return {
        name: json.dumps(
            HandlerFloorFillCandidates().handle(req).model_dump(mode="json"),
            sort_keys=True,
        )
        for name, req in scenarios().items()
    }


class Bus:
    """Carry one JSON request to the node's consumer and the JSON decision back."""

    async def start(self) -> None: ...

    async def call(self, payload: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def stop(self) -> None: ...


class InmemoryBus(Bus):
    def __init__(self) -> None:
        self.bus = EventBusInmemory(environment="floor-fill-parity", group="parity")
        self.waiting: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self.prefix = uuid.uuid4().hex

    async def start(self) -> None:
        await self.bus.start()

        async def serve(message: Any) -> None:
            reply = decide(json.loads(message.value))
            await self.bus.publish(
                f"{self.prefix}.res", None, json.dumps(reply).encode()
            )

        async def answer(message: Any) -> None:
            reply = json.loads(message.value)
            self.waiting.pop(reply["id"]).set_result(reply)

        await self.bus.subscribe(
            f"{self.prefix}.req", on_message=serve, group_id="serve"
        )
        await self.bus.subscribe(
            f"{self.prefix}.res", on_message=answer, group_id="ask"
        )

    async def call(self, payload: dict[str, Any]) -> dict[str, Any]:
        future: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )
        self.waiting[payload["id"]] = future
        await self.bus.publish(f"{self.prefix}.req", None, json.dumps(payload).encode())
        return await asyncio.wait_for(future, TIMEOUT_S)

    async def stop(self) -> None:
        await self.bus.close()


class RedpandaBus(Bus):
    def __init__(self, bootstrap: str) -> None:
        self.bootstrap = bootstrap
        self.prefix = f"floor-fill-parity-{uuid.uuid4().hex}"
        self.tasks: list[asyncio.Task[None]] = []
        self.consumers: list[Any] = []
        self.waiting: dict[str, asyncio.Future[dict[str, Any]]] = {}

    async def start(self) -> None:
        from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

        self.producer = AIOKafkaProducer(
            bootstrap_servers=self.bootstrap, security_protocol="PLAINTEXT"
        )
        await self.producer.start()

        async def consume(topic: str, handle: Callable[[dict[str, Any]], Any]) -> None:
            consumer = AIOKafkaConsumer(
                topic,
                bootstrap_servers=self.bootstrap,
                security_protocol="PLAINTEXT",
                group_id=f"{self.prefix}-{topic}",
                auto_offset_reset="earliest",
            )
            await consumer.start()
            self.consumers.append(consumer)
            async for message in consumer:
                await handle(json.loads(message.value))

        async def serve(request_payload: dict[str, Any]) -> None:
            await self.producer.send_and_wait(
                f"{self.prefix}.res", json.dumps(decide(request_payload)).encode()
            )

        async def answer(reply: dict[str, Any]) -> None:
            future = self.waiting.pop(reply["id"], None)
            if future is not None:
                future.set_result(reply)

        self.tasks.append(asyncio.create_task(consume(f"{self.prefix}.req", serve)))
        self.tasks.append(asyncio.create_task(consume(f"{self.prefix}.res", answer)))
        await asyncio.sleep(3.0)  # let the consumers join their topics

    async def call(self, payload: dict[str, Any]) -> dict[str, Any]:
        future: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )
        self.waiting[payload["id"]] = future
        await self.producer.send_and_wait(
            f"{self.prefix}.req", json.dumps(payload).encode()
        )
        return await asyncio.wait_for(future, TIMEOUT_S)

    async def stop(self) -> None:
        for task in self.tasks:
            task.cancel()
        for consumer in self.consumers:
            await consumer.stop()
        await self.producer.stop()


async def run_over(bus: Bus, profile: str, binding: str) -> ModelScenarioRun:
    await bus.start()
    try:
        rows = []
        for name, req in scenarios().items():
            payload = {"id": uuid.uuid4().hex, "request": req.model_dump(mode="json")}
            reply = await bus.call(payload)
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


def make_bus(binding: str) -> Bus:
    if binding == "inmemory":
        return InmemoryBus()
    return RedpandaBus(os.environ[BOOTSTRAP_ENV])


def parity_run(*, profiles: tuple[str, ...]) -> tuple[ModelScenarioRun, ...]:
    chosen = ModelFactoryBindingsOverlay.model_validate(
        {
            "profiles": [p.model_dump() for p in OVERLAY.profiles if p.id in profiles],
            "bindings": {p: OVERLAY.bindings[p] for p in profiles},
            "proof_bindings": list(OVERLAY.proof_bindings),
        }
    )
    return tuple(
        asyncio.run(
            run_over(
                make_bus(chosen.binding_id(profile.id, "BUS")),
                profile.id,
                chosen.binding_id(profile.id, "BUS"),
            )
        )
        for profile in chosen.profiles
    )


def test_floor_fill_candidates_parity_scenarios_decide_what_the_node_says() -> None:
    decisions = {k: json.loads(v) for k, v in in_process().items()}
    assert decisions["fixture:usable"]["verdict"] == "READ"
    assert {line["verdict"] for line in decisions["fixture:usable"]["lines"]} == {
        "STARTABLE"
    }
    assert {line["verdict"] for line in decisions["fixture:needs-ticket"]["lines"]} == {
        "NEEDS-TICKET"
    }
    assert decisions["fixture:held"]["lines"][0]["verdict"] == "AT-CAPACITY"
    assert decisions["fixture:stale"]["unread_cause"] == "old"
    assert decisions["fixture:absent"]["unread_cause"] == "absent"
    assert json.loads(fixture_text())["generated_at"] == "2026-11-02T10:40:00Z"
    met = decisions["criterion:met"]["lines"][0]
    assert [c["task_id"] for c in met["candidates"]] == ["T1"]
    unmet = decisions["criterion:unmet"]["lines"][0]
    assert unmet["candidates"] == []
    assert unmet["not_ready"][0]["unmet"] == ["piece 2 WORKING (reads NOT WORKING)"]
    integrating = decisions["integrating:item"]
    assert integrating["verdict"] == "READ"
    assert integrating["lines"][0]["not_ready"][0]["cause"] == "integrating"
    assert [s["lane"] for s in integrating["stopped"]] == ["lane-a"]
    assert decisions["integrating:unknown-state"]["unread_cause"] == "malformed"
    unparseable = decisions["criterion:unparseable"]["lines"][0]
    assert unparseable["not_ready"][0]["cause"].startswith("start-when-unparseable:")


def test_floor_fill_candidates_parity_the_child_ticket_lane_is_a_candidate_only_with_the_snapshot_children() -> (
    None
):
    decisions = {k: json.loads(v) for k, v in in_process().items()}
    listed = decisions["stopped:child-ticket-lane"]
    assert listed["stopped_verdict"] == "STARTABLE"
    assert [c["lane"] for c in listed["stopped"]] == [STOPPED_LANE]
    assert listed["stopped"][0]["ticket"] == CHILD
    assert decisions["stopped:no-snapshot-children"]["stopped"] == []


def test_floor_fill_candidates_parity_in_memory_bus_matches_the_in_process_decisions() -> (
    None
):
    [run] = parity_run(profiles=("inmemory",))
    assert {row.key: row.content for row in run.rows} == in_process()


@pytest.mark.kafka
@pytest.mark.integration
def test_floor_fill_candidates_parity_redpanda_overlay_matches_the_in_memory_overlay() -> (
    None
):
    if not os.environ.get(BOOTSTRAP_ENV):
        pytest.skip(f"Kafka not reachable: {BOOTSTRAP_ENV} names no lab broker")
    runs = parity_run(profiles=("inmemory", "redpanda"))
    result = compare_runs(runs)
    assert result.passed, result.first_difference
    assert {row.key: row.content for row in runs[1].rows} == in_process()
    assert len(runs[1].rows) == len(scenarios())
