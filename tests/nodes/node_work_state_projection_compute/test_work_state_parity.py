# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The work-state projection projects the same items on the in-memory bus and on a Redpanda bus (OMN-22935 AC1).

Each scenario request goes as a JSON message on the bus under test to the projection's consumer, which rebuilds the
request, runs the handler and publishes the projection (the golden chain) or, for a request that does not validate, a
typed error naming the exception class (the error chain) back. The parity run's own comparison (`compare_runs`) judges
the in-memory run against the Redpanda run.

The in-memory leg runs everywhere. The Redpanda leg runs against the plaintext lab broker named in
`WORK_STATE_PARITY_BOOTSTRAP` (one the caller started for the proof, never the dev lane's):

    WORK_STATE_PARITY_BOOTSTRAP=<host:port> uv run pytest -m "work_state_parity and kafka" -q
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from pydantic import ValidationError

from omnimarket.events.work_state_projection import (
    ModelWorkStateRequest,
)
from omnimarket.models.model_scenario_row import ModelScenarioRow
from omnimarket.models.model_scenario_run import ModelScenarioRun
from omnimarket.nodes.node_binding_parity_orchestrator.handlers.handler_compare_runs import (
    compare_runs,
)
from omnimarket.nodes.node_binding_parity_orchestrator.overlay import (
    ModelFactoryBindingsOverlay,
)
from omnimarket.nodes.node_work_state_projection_compute.handlers.handler_work_state_projection import (
    HandlerWorkStateProjection,
)

pytestmark = [pytest.mark.unit, pytest.mark.work_state_parity]

BOOTSTRAP_ENV = "WORK_STATE_PARITY_BOOTSTRAP"
TIMEOUT_S = 30.0
T0 = datetime(2026, 11, 7, 13, 0, tzinfo=UTC)
RULES = yaml.safe_load(
    (
        Path(__file__).resolve().parents[3]
        / "src/omnimarket/nodes/node_work_state_projection_compute/overlay.example.yaml"
    ).read_text(encoding="utf-8")
)
OVERLAY = ModelFactoryBindingsOverlay.model_validate(
    {
        "profiles": [{"id": "inmemory", "baseline": True}, {"id": "redpanda"}],
        "bindings": {"inmemory": {"BUS": "inmemory"}, "redpanda": {"BUS": "redpanda"}},
        "proof_bindings": ["inmemory", "redpanda"],
    }
)


def _row(row_type: str, *cells: str, minutes_ago: int) -> dict[str, Any]:
    stamp = T0 - timedelta(minutes=minutes_ago)
    text = " | ".join((stamp.strftime("%Y-%m-%dT%H:%M:%SZ"), row_type, *cells, "text"))
    found = dict(c.split("=", 1) for c in cells)
    return {
        "stamp": stamp.isoformat(),
        "row_type": row_type,
        "lane": found.get("lane"),
        "cells": found,
        "raw": text,
    }


def _request(**fields: Any) -> dict[str, Any]:
    return {
        "now": T0.isoformat(),
        "rules": RULES,
        "scope": {"in_scope_projects": ["Zephyr"]},
    } | fields


def golden_scenario() -> list[tuple[str, dict[str, Any]]]:
    """One named request per way an item can stand, and one request that does not validate."""
    return [
        (
            "00:new-project-available",
            _request(
                tickets=[
                    {
                        "ticket_id": "ZEP-1",
                        "state_type": "completed",
                        "project": "Zephyr",
                    },
                    {
                        "ticket_id": "ZEP-2",
                        "state_type": "backlog",
                        "project": "Zephyr",
                        "requires": ["ZEP-1"],
                    },
                ]
            ),
        ),
        (
            "01:stopped-lane-pushed-branch",
            _request(
                tickets=[
                    {"ticket_id": "ZEP-7", "state_type": "started", "project": "Zephyr"}
                ],
                ledger_rows=[
                    _row("CLAIM", "lane=fix-7", "ticket=ZEP-7", minutes_ago=90),
                    _row(
                        "TERMINAL",
                        "lane=fix-7",
                        "outcome=partial",
                        "branch=zep-7",
                        "head=abc",
                        minutes_ago=20,
                    ),
                ],
            ),
        ),
        (
            "02:red-check-owned",
            _request(
                pull_requests=[
                    {
                        "repo": "omnimarket",
                        "number": 42,
                        "state": "OPEN",
                        "red_required_checks": ["Lint", "Tests"],
                    }
                ],
                ledger_rows=[
                    _row(
                        "CLAIM",
                        "lane=pr-fix",
                        "owns=OmniNode-ai/omnimarket#42",
                        minutes_ago=5,
                    )
                ],
            ),
        ),
        (
            "03:plan-task-blocked",
            _request(
                plan_tasks=[
                    {"plan": "p.md", "task_id": "T1"},
                    {"plan": "p.md", "task_id": "T2", "requires": ["plan:p.md#T1"]},
                ]
            ),
        ),
        ("04:error-invalid-request", _request(rules={"ticket_id_pattern": "x"})),
    ]


def decide(payload: dict[str, Any]) -> dict[str, Any]:
    """The projection's consumer: rebuild the request from the message and run the handler, or name the error."""
    try:
        request = ModelWorkStateRequest.model_validate(payload["request"])
    except ValidationError as exc:
        return {
            "id": payload["id"],
            "error": type(exc).__name__,
            "count": exc.error_count(),
        }
    out = HandlerWorkStateProjection().handle(request)
    return {"id": payload["id"], "output": out.model_dump(mode="json")}


def _content(reply: dict[str, Any]) -> str:
    body = {k: v for k, v in reply.items() if k != "id"}
    return json.dumps(body, sort_keys=True)


def in_process() -> dict[str, str]:
    return {
        name: _content(decide({"id": name, "request": req}))
        for name, req in golden_scenario()
    }


class Bus:
    async def start(self) -> None: ...

    async def call(self, payload: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def stop(self) -> None: ...


class InmemoryBus(Bus):
    def __init__(self) -> None:
        self.bus = EventBusInmemory(environment="work-state-parity", group="parity")
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
        self.prefix = f"work-state-parity-{uuid.uuid4().hex}"
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
            rows.append(ModelScenarioRow(key=name, content=_content(reply)))
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
                make_bus(chosen.binding_id(p.id, "BUS")),
                p.id,
                chosen.binding_id(p.id, "BUS"),
            )
        )
        for p in chosen.profiles
    )


def _states(rows: dict[str, str], name: str) -> dict[str, str]:
    out = json.loads(rows[name])["output"]
    return {i["key"]: i["state"] for i in out["items"]}


def test_work_state_parity_golden_chain_projects_what_the_rules_say() -> None:
    rows = in_process()
    assert _states(rows, "00:new-project-available")["ticket:ZEP-2"] == "available"
    assert _states(rows, "01:stopped-lane-pushed-branch")["ticket:ZEP-7"] == "stopped"
    assert (
        _states(rows, "02:red-check-owned")["check:omnimarket#42:Lint"] == "in_progress"
    )
    assert _states(rows, "03:plan-task-blocked")["plan:p.md#T2"] == "blocked"


def test_work_state_parity_error_chain_names_the_validation_error() -> None:
    reply = json.loads(in_process()["04:error-invalid-request"])
    assert reply["error"] == "ValidationError"
    assert reply["count"] > 0


def test_work_state_parity_in_memory_bus_matches_the_in_process_projection() -> None:
    [run] = parity_run(profiles=("inmemory",))
    assert {row.key: row.content for row in run.rows} == in_process()


@pytest.mark.kafka
@pytest.mark.integration
def test_work_state_parity_redpanda_overlay_matches_the_in_memory_overlay() -> None:
    if not os.environ.get(BOOTSTRAP_ENV):
        pytest.skip(f"Kafka not reachable: {BOOTSTRAP_ENV} names no lab broker")
    runs = parity_run(profiles=("inmemory", "redpanda"))
    result = compare_runs(runs)
    assert result.passed, result.first_difference
    assert {row.key: row.content for row in runs[1].rows} == in_process()
    assert len(runs[1].rows) == len(golden_scenario())
