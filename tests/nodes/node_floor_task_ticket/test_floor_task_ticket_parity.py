# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_floor_task_ticket_payload_compute decides the same on the in-memory bus and on a Redpanda bus (G-NODE point 5).

Each scenario's request travels as a JSON message on the bus under test; the node's consumer rebuilds the request,
runs the node and publishes the verdict as a JSON message back. The verdict of every scenario is one row of the run,
and the parity run's own comparison (``compare_runs``) judges the in-memory run against the Redpanda run.

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
from typing import Any

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

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
from omnimarket.nodes.node_floor_task_ticket_effect.handlers import (
    HandlerFloorTaskTicketMint,
)
from omnimarket.nodes.node_floor_task_ticket_effect.models import (
    ModelFloorTaskTicketMintRequest,
)
from omnimarket.nodes.node_floor_task_ticket_effect.tests.support import ScriptedStore
from omnimarket.nodes.node_floor_task_ticket_payload_compute.handlers.handler_floor_task_ticket_payload import (
    HandlerFloorTaskTicketPayload,
)
from omnimarket.nodes.node_floor_task_ticket_payload_compute.models import (
    ModelFloorTaskTicketPayloadRequest,
)
from omnimarket.nodes.node_floor_task_ticket_payload_compute.tests.support import (
    LINE,
    PLAN,
    PLAN_WITHOUT_TICKET,
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


PAYLOAD_SCENARIOS: dict[str, ModelFloorTaskTicketPayloadRequest] = {
    "payload:ready": ModelFloorTaskTicketPayloadRequest(
        line_id=LINE, task_id="T1", plan_text=PLAN
    ),
    "payload:needs-spec-falsifier": ModelFloorTaskTicketPayloadRequest(
        line_id=LINE, task_id="T2", plan_text=PLAN
    ),
    "payload:needs-spec-integration": ModelFloorTaskTicketPayloadRequest(
        line_id=LINE, task_id="T4", plan_text=PLAN
    ),
    "payload:no-parent": ModelFloorTaskTicketPayloadRequest(
        line_id=LINE, task_id="T1", plan_text=PLAN_WITHOUT_TICKET
    ),
    "payload:plan-unread": ModelFloorTaskTicketPayloadRequest(
        line_id=LINE, task_id="T1", plan_text=None
    ),
}


def mint_row() -> dict[str, Any]:
    """The mint of the ready task on an empty in-memory store, then again: one create, then the ticket reused."""
    payload = (
        HandlerFloorTaskTicketPayload()
        .handle(PAYLOAD_SCENARIOS["payload:ready"])
        .payload
    )
    assert payload is not None
    store = ScriptedStore()
    handler = HandlerFloorTaskTicketMint(store)
    request = ModelFloorTaskTicketMintRequest(
        **payload.model_dump(), mint_timeout_seconds=5.0
    )
    first = handler.handle(request).model_dump(mode="json")
    second = handler.handle(request).model_dump(mode="json")
    return {"first": first, "second": second, "creates": store.creates}


def scenarios() -> dict[str, ModelFloorTaskTicketPayloadRequest]:
    """The payload compute's verdicts: a payload, each NEEDS-SPEC cause, NO-PARENT and PLAN-UNREAD."""
    return dict(PAYLOAD_SCENARIOS)


def decide(payload: dict[str, Any]) -> dict[str, Any]:
    result = HandlerFloorTaskTicketPayload().handle(
        ModelFloorTaskTicketPayloadRequest.model_validate(payload["request"])
    )
    return {"id": payload["id"], "decision": result.model_dump(mode="json")}


def in_process() -> dict[str, str]:
    return {
        name: json.dumps(
            HandlerFloorTaskTicketPayload().handle(req).model_dump(mode="json"),
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
        self.bus = EventBusInmemory(
            environment="floor-task-ticket-parity", group="parity"
        )
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
        self.prefix = f"floor-task-ticket-parity-{uuid.uuid4().hex}"
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


def test_floor_task_ticket_parity_scenarios_decide_what_the_node_says() -> None:
    decisions = {k: json.loads(v) for k, v in in_process().items()}
    assert decisions["payload:ready"]["verdict"] == "PAYLOAD"
    assert decisions["payload:ready"]["payload"]["floor_key"] == f"floor:{LINE}:T1"
    assert decisions["payload:needs-spec-falsifier"]["missing"] == ["falsifier:2"]
    assert decisions["payload:needs-spec-integration"]["missing"] == [
        "integration-check"
    ]
    assert decisions["payload:no-parent"]["verdict"] == "NO-PARENT"
    assert decisions["payload:plan-unread"]["verdict"] == "PLAN-UNREAD"


def test_floor_task_ticket_parity_the_mint_creates_once_and_then_reuses() -> None:
    row = mint_row()
    assert row["creates"] == 1
    assert row["first"]["state"] == "ticket"
    assert row["first"]["reused"] is False
    assert row["second"]["ticket"] == row["first"]["ticket"]
    assert row["second"]["reused"] is True


def test_floor_task_ticket_parity_in_memory_bus_matches_the_in_process_verdicts() -> (
    None
):
    [run] = parity_run(profiles=("inmemory",))
    assert {row.key: row.content for row in run.rows} == in_process()


@pytest.mark.kafka
@pytest.mark.integration
def test_floor_task_ticket_parity_redpanda_overlay_matches_the_in_memory_overlay() -> (
    None
):
    if not os.environ.get(BOOTSTRAP_ENV):
        pytest.skip(f"Kafka not reachable: {BOOTSTRAP_ENV} names no lab broker")
    runs = parity_run(profiles=("inmemory", "redpanda"))
    result = compare_runs(runs)
    assert result.passed, result.first_difference
    assert {row.key: row.content for row in runs[1].rows} == in_process()
    assert len(runs[1].rows) == len(scenarios())


def test_floor_task_ticket_parity_comparison_fails_on_a_differing_verdict() -> None:
    """The control: a leg that decides one scenario differently is a failed comparison, so a pass reads something."""
    [run] = parity_run(profiles=("inmemory",))
    changed = tuple(
        row.model_copy(
            update={
                "content": row.content.replace(
                    '"verdict": "PAYLOAD"', '"verdict": "NO-PARENT"'
                )
            }
        )
        for row in run.rows
    )
    assert changed != run.rows
    other = run.model_copy(
        update={"profile_id": "redpanda", "binding": "redpanda", "rows": changed}
    )
    result = compare_runs((run, other))
    assert not result.passed
    assert "payload:ready" in str(result.first_difference)
