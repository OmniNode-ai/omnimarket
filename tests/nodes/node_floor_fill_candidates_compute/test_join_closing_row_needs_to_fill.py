# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Join test for the needs cell of a stopped lane's closing row, writer to fill (OMN-23416): row writer -> stopped compute -> fill.

Three pieces meet on one cell. The lane start orchestrator's row writer appends the TERMINAL row of a lane that ended
(lane ``startnode``'s node, with the ``needs=`` cell the close node decides, OMN-23417); the stopped-lane compute
(``node_factory_floor_compute``, lane ``contin``'s consumer) lists the lane through the floor effect's ledger parser and
writes the cell into ``floor.stopped[].needs`` of the floor record; the fill's candidates compute
(``node_floor_fill_candidates_compute``) reads that entry and puts the cell in the continuation's brief.

Each scenario's final message travels as a JSON message on the bus under test; the served side runs the orchestrator
over the scripted transport and returns the TERMINAL row it wrote; the reading side parses that row, asks the stopped
compute, builds the floor record from what it listed and asks the candidates compute for the brief.

The join reads ``passed`` when the cell the writer wrote is the ask the continuation's brief carries. A planted rename of
the cell key on the row, or of the field in the floor record, reads ``broken`` (the failing controls); a rename of the
reader's own field is pinned by ``test_floor_fill_stopped_needs.py``. The in-memory leg runs everywhere; the Redpanda
leg runs against the lab broker named in ``FLOOR_FILL_PARITY_BOOTSTRAP``:

    FLOOR_FILL_PARITY_BOOTSTRAP=<host:port> uv run pytest -m "floor_fill_parity and kafka" -q
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
from omnimarket.nodes.node_floor_fill_candidates_compute.handlers.handler_floor_fill_candidates import (
    HandlerFloorFillCandidates,
)
from omnimarket.nodes.node_floor_fill_candidates_compute.tests.support import (
    NOW,
    minutes_before,
)
from omnimarket.nodes.node_floor_fill_candidates_compute.tests.support import (
    request as fill_request,
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

pytestmark = [pytest.mark.unit, pytest.mark.floor_fill_parity]

BOOTSTRAP_ENV = "FLOOR_FILL_PARITY_BOOTSTRAP"
TIMEOUT_S = 30.0
TICKET = "OMN-22848"
DELEGATION = "DELEGATION delegated=0 reason=read-only-lane"
SCOPE = ModelGoalScope(ruling="test", in_scope_tickets=(TICKET,))
ASK = "The predecessor asked for next: "

# scenario -> (final message, the ask the continuation's brief must carry, or "" for none, the lane's place in the fill)
SCENARIOS: dict[str, tuple[str, str, str]] = {
    "typed-rerun": (
        "The required check failed on a runner timeout.\nLANE_RESULT outcome=blocked needs=rerun:omnibase_core#2302\n"
        + DELEGATION,
        "rerun:omnibase_core#2302",
        "continued",
    ),
    "typed-update-branch-json": (
        '{"outcome": "partial", "needs": "update-branch:omnimarket#5001"}\n'
        + DELEGATION,
        "update-branch:omnimarket#5001",
        "continued",
    ),
    "unparsed-but-names-a-blocker": (
        "I could not push.\nBLOCKER: someone must rerun the check\nLANE_RESULT outcome=blocked\n"
        + DELEGATION,
        "",
        "continued",
    ),
    "handed-off": (
        "Opened omnimarket#1.\nLANE_RESULT outcome=handed-off\n" + DELEGATION,
        "",
        "not-stopped",
    ),
}


def serve_close(message: str) -> dict[str, str]:
    """The writer side: one lane ends with ``message``; return the CLAIM and the TERMINAL the orchestrator wrote."""
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


def floor_record(stopped: list[dict[str, Any]]) -> str:
    """The floor record the floor effect publishes, as far as the fill reads it, from the stopped lanes just listed."""
    return json.dumps(
        {
            "generated_at": minutes_before(NOW, 5),
            "lines": [],
            "floor": {"lanes": [], "stopped": stopped},
        }
    )


def read_back(
    rows: dict[str, str],
    wire: Callable[[list[dict[str, Any]]], list[dict[str, Any]]] = lambda lanes: lanes,
) -> tuple[str, str]:
    """The reading side: the floor effect's parser, the stopped compute, then the fill's candidates compute.

    Returns the ask the continuation's brief carries (empty for none) and the lane's place.
    """
    parsed = parse_ledger_text(f"L1 {rows['claim']}\nL2 {rows['terminal']}")
    made = floor_request(ledger=parsed, goal_scope=SCOPE)
    asked = made.model_copy(update={"generated_at": T0 + timedelta(minutes=5)})
    listed, _, _ = build_stopped(asked)
    if not listed:
        return "", "not-stopped"
    lanes = wire([lane.model_dump(mode="json") for lane in listed])
    result = HandlerFloorFillCandidates().handle(
        fill_request(floor_record=floor_record(lanes))
    )
    [lane] = result.stopped
    start = lane.brief.find(ASK)
    if start < 0:
        return "", "continued"
    return lane.brief[start + len(ASK) :].splitlines()[0], "continued"


def break_rename_cell_on_the_row(rows: dict[str, str]) -> dict[str, str]:
    """Planted rename on the row: the writer's ``needs=`` cell is spelled ``need=``."""
    return {k: v.replace(" | needs=", " | need=") for k, v in rows.items()}


def break_rename_field_in_the_record(
    lanes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Planted rename in the floor record: the stopped lane's ``needs`` field is spelled ``need``."""
    return [
        {("need" if k == "needs" else k): v for k, v in lane.items()} for lane in lanes
    ]


def judge(
    replies: dict[str, dict[str, str]],
    row_wire: Callable[[dict[str, str]], dict[str, str]] = lambda rows: rows,
    record_wire: Callable[
        [list[dict[str, Any]]], list[dict[str, Any]]
    ] = lambda lanes: lanes,
) -> None:
    """The join's reading: every scenario's cell reaches the brief; raises AssertionError on the first that does not."""
    for name, (_, want_ask, want_place) in SCENARIOS.items():
        got_ask, got_place = read_back(row_wire(replies[name]), record_wire)
        assert got_ask == want_ask, f"{name}: brief ask {got_ask!r} != {want_ask!r}"
        assert got_place == want_place, f"{name}: place {got_place!r} != {want_place!r}"


class Bus:
    """Carry one JSON request to the producer's consumer and the written rows back."""

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


def test_golden_the_cell_the_writer_wrote_is_the_ask_the_continuation_brief_carries() -> (
    None
):
    judge(in_process())


def test_golden_in_memory_bus_rows_match_the_in_process_rows() -> None:
    replies = over("inmemory")
    assert replies == in_process()
    judge(replies)


def test_error_an_unreadable_cell_names_no_ask_but_the_lane_is_still_continued() -> (
    None
):
    """The writer keeps a cell it cannot read as ``unparsed``; the brief names no ask and the lane is still continued."""
    rows = in_process()["unparsed-but-names-a-blocker"]
    assert "needs=unparsed" in rows["terminal"]
    assert read_back(rows) == ("", "continued")


@pytest.mark.parametrize(
    ("row_wire", "record_wire"),
    [
        (break_rename_cell_on_the_row, lambda lanes: lanes),
        (lambda rows: rows, break_rename_field_in_the_record),
    ],
    ids=["rename-cell-on-the-row", "rename-field-in-the-record"],
)
def test_control_a_planted_rename_breaks_the_join(
    row_wire: Callable[[dict[str, str]], dict[str, str]],
    record_wire: Callable[[list[dict[str, Any]]], list[dict[str, Any]]],
) -> None:
    replies = in_process()
    judge(
        replies
    )  # the same fold reads passed without the break, so the red is the break's
    with pytest.raises(AssertionError):
        judge(replies, row_wire, record_wire)


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
        judge(replies, break_rename_cell_on_the_row)
