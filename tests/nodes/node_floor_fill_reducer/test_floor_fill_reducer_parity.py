# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The floor fill reducer decides the same on the in-memory bus and on a Redpanda bus (plan T12, G-NODE point 5).

The golden scenario of one startable line is a list of observations. The orchestrator side
holds the row: it sends the stored row and one observation as a JSON message on the bus
under test, the reducer's consumer rebuilds the request, runs the handler and publishes
the output as a JSON message back, and the returned row is the next request's row. The
decision projection of every step is one row of the run, and the parity run's own
comparison (``compare_runs``) judges the in-memory run against the Redpanda run.

The in-memory leg runs everywhere. The Redpanda leg runs against the broker the lab names
in ``FLOOR_FILL_PARITY_BOOTSTRAP`` (a plaintext lab broker the caller started for the
proof, never the dev lane's):

    FLOOR_FILL_PARITY_BOOTSTRAP=<host:port> uv run pytest -m "floor_fill_parity and kafka" -q

This file lives under ``tests/`` and not beside the node: it uses the parity orchestrator's
run and row models, and a file under ``src/`` that imports another node's models package
is a cross-node reach-in (``tests/test_no_cross_node_reach_in.py`` reads ``src/``).
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.models.floor_fill import (
    ModelFloorFillReduceInput,
    ModelFloorFillState,
)
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
from omnimarket.nodes.node_floor_fill_reducer.handlers.handler_floor_fill_reducer import (
    HandlerFloorFillReducer,
    lane_name,
)

pytestmark = [pytest.mark.unit, pytest.mark.floor_fill_parity]

BOOTSTRAP_ENV = "FLOOR_FILL_PARITY_BOOTSTRAP"
TIMEOUT_S = 30.0
KEY = "floor:line-a:task-1"
OTHER = "floor:line-a:task-2"
T0 = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
OVERLAY = ModelFactoryBindingsOverlay.model_validate(
    {
        "profiles": [{"id": "inmemory", "baseline": True}, {"id": "redpanda"}],
        "bindings": {"inmemory": {"BUS": "inmemory"}, "redpanda": {"BUS": "redpanda"}},
        "proof_bindings": ["inmemory", "redpanda"],
    }
)


def _at(minutes: float) -> str:
    return (T0 + timedelta(minutes=minutes)).isoformat()


def golden_scenario() -> list[dict[str, Any]]:
    """The observations of one startable line: ticket, reserve, confirm, end, expire, reoffer."""
    lane1, lane2 = lane_name(KEY, 1), lane_name(KEY, 2)
    return [
        {"kind": "TicketStored", "key": KEY, "ticket": "OMN-1001"},
        {"kind": "TicketStored", "key": OTHER, "ticket": "OMN-1002"},
        {
            "kind": "ReserveRequested",
            "key": KEY,
            "max_fill_lanes_per_line": 1,
            "slot_budget": 1,
            "landing_only": False,
            "stop_signal": False,
            "sent_window_minutes": 30,
            "now": _at(0),
        },
        {
            "kind": "ReserveRequested",
            "key": OTHER,
            "max_fill_lanes_per_line": 1,
            "slot_budget": 1,
            "landing_only": False,
            "stop_signal": False,
            "sent_window_minutes": 30,
            "now": _at(0),
        },
        {
            "kind": "ReservedChecked",
            "key": KEY,
            "attempt": 1,
            "lane": lane1,
            "claim_found": True,
            "reserved_timeout_seconds": 600,
            "now": _at(2),
        },
        {
            "kind": "ReservedChecked",
            "key": KEY,
            "attempt": 1,
            "lane": lane1,
            "claim_found": False,
            "reserved_timeout_seconds": 600,
            "now": _at(3),
        },
        {
            "kind": "SentChecked",
            "key": KEY,
            "attempt": 1,
            "lane": lane1,
            "lane_ended": True,
            "sent_window_minutes": 30,
            "now": _at(10),
        },
        {
            "kind": "SentChecked",
            "key": KEY,
            "attempt": 1,
            "lane": lane1,
            "lane_ended": True,
            "sent_window_minutes": 30,
            "now": _at(40),
        },
        {
            "kind": "ReserveRequested",
            "key": KEY,
            "max_fill_lanes_per_line": 2,
            "slot_budget": 1,
            "landing_only": False,
            "stop_signal": False,
            "sent_window_minutes": 30,
            "now": _at(41),
        },
        {
            "kind": "StartFailed",
            "key": KEY,
            "attempt": 2,
            "lane": lane2,
        },
    ]


def decide(payload: dict[str, Any]) -> dict[str, Any]:
    """The reducer's consumer: rebuild the request from the message, run the handler."""
    out = HandlerFloorFillReducer().handle(
        ModelFloorFillReduceInput.model_validate(payload["request"])
    )
    return {"id": payload["id"], "output": out.model_dump(mode="json")}


def in_process() -> dict[str, str]:
    state = ModelFloorFillState()
    rows: dict[str, str] = {}
    for index, observation in enumerate(golden_scenario()):
        out = HandlerFloorFillReducer().handle(
            ModelFloorFillReduceInput(state=state, observation=observation)
        )
        state = out.state
        rows[f"{index:02d}:{observation['kind']}"] = json.dumps(
            out.model_dump(mode="json"), sort_keys=True
        )
    return rows


class Bus:
    """Carry one JSON request to the reducer's consumer and the JSON output back."""

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

        async def serve(request: dict[str, Any]) -> None:
            await self.producer.send_and_wait(
                f"{self.prefix}.res", json.dumps(decide(request)).encode()
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
        state = ModelFloorFillState().model_dump(mode="json")
        rows = []
        for index, observation in enumerate(golden_scenario()):
            payload = {
                "id": uuid.uuid4().hex,
                "request": {"state": state, "observation": observation},
            }
            reply = await bus.call(payload)
            state = reply["output"]["state"]
            rows.append(
                ModelScenarioRow(
                    key=f"{index:02d}:{observation['kind']}",
                    content=json.dumps(reply["output"], sort_keys=True),
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


def test_reducer_parity_the_golden_scenario_decides_what_the_rules_say() -> None:
    rows = {k: json.loads(v) for k, v in in_process().items()}
    assert [r["trigger"] for r in rows.values()] == [
        "ticket_stored",
        "ticket_stored",
        "reserve_admitted",
        None,  # the second key of the line is refused: capacity
        "confirmed",
        None,  # a late check with no claim after the confirm: wrong-state
        "sent_ended",
        "sent_expired",
        "reserve_admitted",
        "start_failed",
    ]
    assert rows["03:ReserveRequested"]["dropped_reason"] == "capacity"
    assert rows["05:ReservedChecked"]["dropped_reason"] == "wrong-state"
    assert rows["09:StartFailed"]["state"]["seq"] == 8


def test_reducer_parity_in_memory_bus_matches_the_in_process_decisions() -> None:
    [run] = parity_run(profiles=("inmemory",))
    assert {row.key: row.content for row in run.rows} == in_process()


def test_reducer_parity_the_golden_scenario_over_dotted_floor_task_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The floor's own task ids carry a dot (``T0.1``): the bus legs decide them as the rules do."""
    monkeypatch.setitem(globals(), "KEY", "floor:line-a:T0.1")
    monkeypatch.setitem(globals(), "OTHER", "floor:line-a:CM.0")
    [run] = parity_run(profiles=("inmemory",))
    assert {row.key: row.content for row in run.rows} == in_process()
    assert len(run.rows) == len(golden_scenario())
    rows = {k: json.loads(v) for k, v in in_process().items()}
    assert rows["04:ReservedChecked"]["trigger"] == "confirmed"
    assert rows["04:ReservedChecked"]["state"]["entries"][KEY]["lane"] == lane_name(
        KEY, 1
    )


class SeqCorruptingBus(InmemoryBus):
    """The injected break ``bus-corrupts-seq``: the transport raises the row's ``seq`` of one reply."""

    async def call(self, payload: dict[str, Any]) -> dict[str, Any]:
        reply = await super().call(payload)
        if reply["output"]["trigger"] == "confirmed":
            reply["output"]["state"]["seq"] += 1
        return reply


def test_reducer_parity_the_injected_break_bus_corrupts_seq_is_judged_unequal() -> None:
    """The parity reading can fail: a transport that changes one decision is not equal to the baseline."""
    baseline = asyncio.run(run_over(InmemoryBus(), "inmemory", "inmemory"))
    uncorrupted = asyncio.run(run_over(InmemoryBus(), "inmemory", "inmemory"))
    broken = asyncio.run(run_over(SeqCorruptingBus(), "inmemory", "inmemory"))
    # The control: the same provenance with no corruption passes, so only the seq differs.
    assert compare_runs((baseline, uncorrupted)).passed
    result = compare_runs((baseline, broken))
    assert result.comparisons[0].provenance_errors == ()
    assert not result.passed
    assert "ReservedChecked" in result.first_difference


def test_reducer_parity_a_run_whose_binding_is_not_its_profile_is_judged_on_provenance() -> (
    None
):
    """The provenance rejection is separate from the row diagnostic, so the break test above must not rely on it."""
    baseline = asyncio.run(run_over(InmemoryBus(), "inmemory", "inmemory"))
    mismatched = asyncio.run(run_over(InmemoryBus(), "redpanda", "inmemory"))
    result = compare_runs((baseline, mismatched))
    assert not result.passed
    assert result.comparisons[0].provenance_errors
    assert result.first_difference.startswith("provenance:")


@pytest.mark.kafka
@pytest.mark.integration
def test_reducer_parity_redpanda_overlay_matches_the_in_memory_overlay() -> None:
    if not os.environ.get(BOOTSTRAP_ENV):
        pytest.skip(f"Kafka not reachable: {BOOTSTRAP_ENV} names no lab broker")
    runs = parity_run(profiles=("inmemory", "redpanda"))
    result = compare_runs(runs)
    assert result.passed, result.first_difference
    assert {row.key: row.content for row in runs[1].rows} == in_process()
    assert len(runs[1].rows) == len(golden_scenario())
