# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The floor fill lines compute words the same lines on the in-memory bus and on a Redpanda bus (plan T4, G-NODE point 5).

The four-line scenario is a list of requests, each the request the fill orchestrator builds
at pass step S6: every start failed, every start worked, no free slots, the open-count
brake, a stop signal, an unread source, and an unresolved reservation. Each request goes
as a JSON message on the bus under test, the compute's consumer rebuilds it, runs the
handler and publishes the findings as a JSON message back. The printed lines of every
request are one row of the run, and the parity run's own comparison (``compare_runs``)
judges the in-memory run against the Redpanda run.

The in-memory leg runs everywhere. The Redpanda leg runs against the broker the lab names
in ``FLOOR_FILL_PARITY_BOOTSTRAP`` (a plaintext lab broker the caller started for the
proof, never the dev lane's):

    FLOOR_FILL_PARITY_BOOTSTRAP=<host:port> uv run pytest -m "floor_fill_parity and kafka" -q
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
    ModelFloorFillLinesRequest,
)
from omnimarket.models.model_scenario_row import ModelScenarioRow
from omnimarket.models.model_scenario_run import ModelScenarioRun
from omnimarket.nodes.node_binding_parity_orchestrator.handlers.handler_compare_runs import (
    compare_runs,
)
from omnimarket.nodes.node_binding_parity_orchestrator.overlay import (
    ModelFactoryBindingsOverlay,
)
from omnimarket.nodes.node_floor_fill_lines_compute.handlers.handler_floor_fill_lines import (
    HandlerFloorFillLines,
)

pytestmark = [pytest.mark.unit, pytest.mark.floor_fill_parity]

BOOTSTRAP_ENV = "FLOOR_FILL_PARITY_BOOTSTRAP"
TIMEOUT_S = 30.0
T0 = datetime(2026, 11, 3, 4, 0, tzinfo=UTC)
LINES = ("alpha", "bravo", "charlie", "delta")
OVERLAY = ModelFactoryBindingsOverlay.model_validate(
    {
        "profiles": [{"id": "inmemory", "baseline": True}, {"id": "redpanda"}],
        "bindings": {"inmemory": {"BUS": "inmemory"}, "redpanda": {"BUS": "redpanda"}},
        "proof_bindings": ["inmemory", "redpanda"],
    }
)


def _request(**fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "unread": [],
        "lines": [
            {
                "line_id": line,
                "verdict": "STARTABLE",
                "next_task_id": f"{line}-t1",
                "ticket": f"T-{line}",
            }
            for line in LINES
        ],
        "entries": [],
        "now": T0.isoformat(),
        "reserved_timeout_seconds": 600,
        "unresolved_keys": [],
        "start_results": [],
        "free_slots_after": 20,
        "hold": "none",
    }
    return base | fields


def _results(kind: str, **cell: str) -> list[dict[str, Any]]:
    return [{"key": f"floor:{line}:{line}-t1", "kind": kind, **cell} for line in LINES]


def golden_scenario() -> list[tuple[str, dict[str, Any]]]:
    """The four-line scenario, one named request per way a pass can end."""
    reserved = {
        "key": "floor:alpha:alpha-t1",
        "line_id": "alpha",
        "state": "RESERVED",
        "lane": "floor-alpha-a1",
        "reserved_at": (T0 - timedelta(minutes=17)).isoformat(),
    }
    return [
        ("00:all-failed", _request(start_results=_results("failed", cause="exit-3"))),
        ("01:all-started", _request(start_results=_results("started", lane="floor-x"))),
        ("02:no-free-slots", _request(free_slots_after=0)),
        ("03:open-count", _request(hold="open-count")),
        ("04:stop-signal", _request(hold="stop-signal:operator")),
        ("05:pool-unread", _request(unread=["pool:no host answered"])),
        (
            "06:reserved-unresolved",
            _request(
                lines=[],
                entries=[reserved],
                unresolved_keys=["floor:alpha:alpha-t1"],
            ),
        ),
    ]


def decide(payload: dict[str, Any]) -> dict[str, Any]:
    """The compute's consumer: rebuild the request from the message, run the handler."""
    out = HandlerFloorFillLines().handle(
        ModelFloorFillLinesRequest.model_validate(payload["request"])
    )
    return {"id": payload["id"], "output": out.model_dump(mode="json")}


def in_process() -> dict[str, str]:
    rows: dict[str, str] = {}
    for name, req in golden_scenario():
        out = HandlerFloorFillLines().handle(
            ModelFloorFillLinesRequest.model_validate(req)
        )
        rows[name] = json.dumps(out.model_dump(mode="json"), sort_keys=True)
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
        rows = []
        for name, req in golden_scenario():
            reply = await bus.call({"id": uuid.uuid4().hex, "request": req})
            rows.append(
                ModelScenarioRow(
                    key=name, content=json.dumps(reply["output"], sort_keys=True)
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


def _printed(rows: dict[str, str], name: str) -> list[str]:
    out = json.loads(rows[name])
    return [
        f"{f['level'].upper()} {f['loop']}" + (f" | {f['why']}" if f["why"] else "")
        for f in out["findings"]
    ]


def test_floor_fill_lines_parity_the_scenario_prints_what_the_rules_say() -> None:
    rows = in_process()
    assert _printed(rows, "00:all-failed")[0] == (
        "MISSING line-stopped:alpha | next=alpha-t1 ticket=T-alpha slots_free=20 cause=start-failed:exit-3"
    )
    assert len(_printed(rows, "00:all-failed")) == 4
    assert (
        _printed(rows, "01:all-started")[0] == "NOTE line-filled:alpha | lane=floor-x"
    )
    assert (
        _printed(rows, "02:no-free-slots")[0] == "NOTE line-held:alpha | no-free-slots"
    )
    assert _printed(rows, "03:open-count")[3] == "NOTE line-held:delta | open-count"
    assert _printed(rows, "04:stop-signal")[0] == (
        "NOTE line-held:alpha | stop-signal:operator"
    )
    assert _printed(rows, "05:pool-unread") == ["UNKNOWN pool-unread:no host answered"]
    assert _printed(rows, "06:reserved-unresolved") == [
        "MISSING line-reserved:floor:alpha:alpha-t1 | line=alpha age=17m"
    ]


def test_floor_fill_lines_parity_in_memory_bus_matches_the_in_process_lines() -> None:
    [run] = parity_run(profiles=("inmemory",))
    assert {row.key: row.content for row in run.rows} == in_process()


@pytest.mark.kafka
@pytest.mark.integration
def test_floor_fill_lines_parity_redpanda_overlay_matches_the_in_memory_overlay() -> (
    None
):
    if not os.environ.get(BOOTSTRAP_ENV):
        pytest.skip(f"Kafka not reachable: {BOOTSTRAP_ENV} names no lab broker")
    runs = parity_run(profiles=("inmemory", "redpanda"))
    result = compare_runs(runs)
    assert result.passed, result.first_difference
    assert {row.key: row.content for row in runs[1].rows} == in_process()
    assert len(runs[1].rows) == len(golden_scenario())
