# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Join test for the closing row's needs and report cells (OMN-23417): close node -> row writer -> stopped-lane compute.

Three pieces meet on one row. The close node (``node_remote_lane_close_compute``) decides the ``needs=`` and ``report=``
cells; the lane start orchestrator's row writer copies them onto the TERMINAL it appends (lane ``startnode``'s node); the
stopped-lane compute (``node_factory_floor_compute``, lane ``contin``'s consumer) reads the row back through the floor
effect's ledger parser. Each scenario's final message travels as a JSON message on the bus under test; the served side runs
the orchestrator over the scripted transport and returns the TERMINAL row it wrote; the reading side parses that row and
asks the stopped-lane compute.

The join reads ``passed`` when the row the producer wrote is the row the consumer understands: the typed cell round-trips,
and a lane whose row now carries its report is listed with what it names. A planted rename of a cell key or of the cell's
shape on the wire between the two sides reads ``broken`` (the failing controls). The in-memory leg runs everywhere; the
Redpanda leg runs against the lab broker named in ``CLOSING_ROW_PARITY_BOOTSTRAP``:

    CLOSING_ROW_PARITY_BOOTSTRAP=<host:port> uv run pytest -m "closing_row_parity and kafka" -q
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import uuid
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.models.floor_17a.model_floor_17a import ModelGoalScope
from omnimarket.nodes.node_factory_floor_compute.handlers.floor_stopped import (
    build_stopped,
)
from omnimarket.nodes.node_factory_floor_effect.source_loader import (
    parse_ledger_text,
)
from omnimarket.nodes.node_remote_lane_start_effect.adapters.adapter_scripted_transport import (
    ModelScriptedTransportScript,
)
from omnimarket.nodes.node_remote_lane_start_orchestrator.tests.lane_start_fixtures import (
    T0,
    Rig,
    request,
    stream_result,
)
from tests.nodes.node_factory_floor_compute.support import request as floor_request

pytestmark = [pytest.mark.unit, pytest.mark.closing_row_parity]

BOOTSTRAP_ENV = "CLOSING_ROW_PARITY_BOOTSTRAP"
TIMEOUT_S = 30.0
TICKET = "OMN-22848"
DELEGATION = "DELEGATION delegated=0 reason=read-only-lane"
SCOPE = ModelGoalScope(ruling="test", in_scope_tickets=(TICKET,))

# scenario -> (final message, the needs cell the row must carry or "" for none, the lane's place in the stopped listing)
SCENARIOS: dict[str, tuple[str, str, str]] = {
    "typed-rerun": (
        "The required check failed on a runner timeout.\nLANE_RESULT outcome=blocked needs=rerun:omnibase_core#2302\n"
        + DELEGATION,
        "rerun:omnibase_core#2302",
        "listed",
    ),
    "typed-update-branch-json": (
        '{"outcome": "partial", "needs": "update-branch:omnimarket#5001"}\n'
        + DELEGATION,
        "update-branch:omnimarket#5001",
        "listed",
    ),
    "unparsed-but-names-a-blocker": (
        "I could not push.\nBLOCKER: someone must rerun the check\nLANE_RESULT outcome=blocked\n"
        + DELEGATION,
        "unparsed",
        "listed",
    ),
    "unparsed-names-nothing": (
        "The check failed.\nLANE_RESULT outcome=blocked\n" + DELEGATION,
        "unparsed",
        "names-no-branch-ask-or-next-step",
    ),
    "handed-off": (
        "Opened omnimarket#1.\nLANE_RESULT outcome=handed-off\n" + DELEGATION,
        "",
        "not-stopped",
    ),
}


def serve_close(message: str) -> dict[str, str]:
    """The producer side: one lane ends with ``message``; return the CLAIM and the TERMINAL the orchestrator wrote."""
    with tempfile.TemporaryDirectory() as scratch:
        rig = Rig(
            Path(scratch),
            ModelScriptedTransportScript(
                exit_polls=1, log_lines=(stream_result(message),)
            ),
        )
        rig.orchestrator.run(request())
        [claim] = rig.ledger_rows("CLAIM")
        [terminal] = rig.ledger_rows("TERMINAL")
    return {"claim": claim, "terminal": terminal}


def decide(payload: dict[str, Any]) -> dict[str, Any]:
    return {"id": payload["id"], **serve_close(payload["message"])}


def read_back(rows: dict[str, str]) -> tuple[str, str]:
    """The consuming side: the floor effect's parser, then the stopped-lane compute. Returns the needs cell and the listing."""
    # the ledger reader's answer: one `L<line> <row>` per row
    parsed = parse_ledger_text(f"L1 {rows['claim']}\nL2 {rows['terminal']}")
    [ended] = [r for r in parsed if r.kind == "TERMINAL"]
    made = floor_request(ledger=parsed, goal_scope=SCOPE)
    asked = made.model_copy(update={"generated_at": T0 + timedelta(minutes=5)})
    listed, skipped, _ = build_stopped(asked)
    if ended.outcome not in asked.config.stopped.outcomes:
        place = "not-stopped"
    elif listed:
        [lane] = listed
        assert lane.needs == ended.needs
        place = "listed"
    else:
        [skip] = skipped
        place = skip.cause
    return ended.needs, place


def break_rename_needs_key(rows: dict[str, str]) -> dict[str, str]:
    """Planted rename on the wire: the producer's ``needs=`` cell is spelled ``need=``."""
    return {k: v.replace(" | needs=", " | need=") for k, v in rows.items()}


def break_rename_report_key(rows: dict[str, str]) -> dict[str, str]:
    """Planted rename on the wire: the producer's ``report=`` cell is spelled ``summary=``."""
    return {k: v.replace(" | report=", " | summary=") for k, v in rows.items()}


def break_reshape_cell(rows: dict[str, str]) -> dict[str, str]:
    """Planted reshape on the wire: the cell's ``<action>:<target>`` becomes ``<action>/<target>``."""
    out = dict(rows)
    terminal = out["terminal"]
    for action in ("rerun:", "update-branch:"):
        terminal = terminal.replace(f"needs={action}", f"needs={action[:-1]}/")
    out["terminal"] = terminal
    return out


def judge(
    replies: dict[str, dict[str, str]],
    wire: Callable[[dict[str, str]], dict[str, str]] = lambda rows: rows,
) -> None:
    """The join's reading: every scenario's row round-trips; raises AssertionError on the first that does not."""
    for name, (_, want_cell, want_place) in SCENARIOS.items():
        got_cell, got_place = read_back(wire(replies[name]))
        assert got_cell == want_cell, (
            f"{name}: needs cell {got_cell!r} != {want_cell!r}"
        )
        assert got_place == want_place, (
            f"{name}: listing {got_place!r} != {want_place!r}"
        )


class Bus:
    """Carry one JSON request to the producer's consumer and the written rows back."""

    async def start(self) -> None: ...

    async def call(self, payload: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def stop(self) -> None: ...


class InmemoryBus(Bus):
    def __init__(self) -> None:
        self.bus = EventBusInmemory(environment="closing-row-parity", group="parity")
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
        self.prefix = f"closing-row-parity-{uuid.uuid4().hex}"
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


async def run_over(bus: Bus) -> dict[str, dict[str, str]]:
    await bus.start()
    try:
        replies: dict[str, dict[str, str]] = {}
        for name, (message, _, _) in SCENARIOS.items():
            reply = await bus.call({"id": uuid.uuid4().hex, "message": message})
            replies[name] = {"claim": reply["claim"], "terminal": reply["terminal"]}
    finally:
        await bus.stop()
    return replies


def in_process() -> dict[str, dict[str, str]]:
    return {name: serve_close(message) for name, (message, _, _) in SCENARIOS.items()}


def over(binding: str) -> dict[str, dict[str, str]]:
    bus: Bus = (
        InmemoryBus()
        if binding == "inmemory"
        else RedpandaBus(os.environ[BOOTSTRAP_ENV])
    )
    return asyncio.run(run_over(bus))


def test_golden_the_rows_the_producer_writes_are_the_rows_the_stopped_compute_reads() -> (
    None
):
    judge(in_process())


def test_golden_a_report_makes_a_lane_listable_that_its_old_row_could_not() -> None:
    """Before OMN-23417 the row said only ``ended status=blocked``; the report cell is what names the blocker."""
    rows = in_process()["unparsed-but-names-a-blocker"]
    without_report = break_rename_report_key(rows)
    assert read_back(rows)[1] == "listed"
    assert read_back(without_report)[1] == "names-no-branch-ask-or-next-step"


def test_golden_in_memory_bus_rows_match_the_in_process_rows() -> None:
    replies = over("inmemory")
    assert replies == in_process()
    judge(replies)


@pytest.mark.parametrize(
    "wire",
    [break_rename_needs_key, break_rename_report_key, break_reshape_cell],
    ids=lambda f: f.__name__,
)
def test_control_a_planted_rename_or_reshape_breaks_the_join(
    wire: Callable[[dict[str, str]], dict[str, str]],
) -> None:
    replies = in_process()
    judge(
        replies
    )  # the same fold reads passed without the break, so the red is the break's
    with pytest.raises(AssertionError):
        judge(replies, wire)


@pytest.mark.kafka
@pytest.mark.integration
def test_golden_redpanda_bus_rows_match_the_in_memory_rows_and_the_join_reads_passed() -> (
    None
):
    if not os.environ.get(BOOTSTRAP_ENV):
        pytest.skip(f"Kafka not reachable: {BOOTSTRAP_ENV} names no lab broker")
    replies = over("redpanda")
    assert replies == over("inmemory")
    judge(replies)
    with pytest.raises(AssertionError):
        judge(replies, break_rename_needs_key)
