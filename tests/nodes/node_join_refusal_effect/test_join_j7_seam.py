# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seam of join J7 (piece 7 to piece 8): the row the stop drill writes is the row the J7 refusal rule reads (OMN-23420).

The producer side is the real stop drill's ``ledger_row``. The consumer side is the join refusal node over the J7 entry of the
join refusal overlay (spelled here, as it stands in the overlays repository: this repository never reads that one), and the
floor's own receipt fold judges the join. Neither side is stubbed: the test changes the shape of the producer's row and reads
what the consumer does.

* golden: a NOT_WORKING stop drill row that is no drill is answered once with piece 8's typed ``upstream_`` refusal and the
  fold reads J7 as an error join with no finding (before the node runs it reads ``downstream_missing_typed_failure``);
* error: the planted fault (drill=yes), a WORKING row and an unreadable ledger are never answered;
* control: a planted rename of the verdict, drill or reason cell turns the golden expectation red, and a journal that drops
  piece 8 leaves J7 broken;
* parity: the same row crosses a bus (in-memory always, Redpanda when ``JOIN_J7_PARITY_BOOTSTRAP`` names a lab broker) and the
  consumer answers the same refusal as an in-process read.
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

from omnimarket.handlers.emission_sink import InMemoryEmissionSink
from omnimarket.models.floor_17a import (
    ModelFloorReceiptInput,
    ModelFloorReceipts,
    ModelJoinDef,
    ModelNodeEmission,
    ModelPieceDef,
)
from omnimarket.nodes.node_floor_emission_compute.handlers.handler_floor_receipt import (
    HandlerFloorReceipt,
)
from omnimarket.nodes.node_floor_simple_effect.handlers.ledger_reads import (
    piece_events,
)
from omnimarket.nodes.node_join_refusal_effect import HandlerJoinRefusal
from omnimarket.nodes.node_join_refusal_effect.models import ModelJoinRefusalCommand
from omnimarket.nodes.node_join_refusal_effect.overlay import ModelJoinRefusalOverlay
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    FileLedgerRowSource,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ModelLedgerQuery,
    ModelLedgerRead,
)
from omnimarket.nodes.node_stop_drill_effect.handlers.handler_stop_drill import (
    ledger_row,
)
from omnimarket.nodes.node_stop_drill_effect.models import ModelStopDrillOverlay

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[3]
NODES = REPO_ROOT / "src/omnimarket/nodes"
BOOTSTRAP_ENV = "JOIN_J7_PARITY_BOOTSTRAP"
TIMEOUT_S = 30.0
DEADLINE_S = 900
NOW = datetime(2026, 11, 10, 3, 0, tzinfo=UTC)
FAILED_AT = NOW - timedelta(hours=1)
LANE = "stop-drill"
REASON = "stop-accepted-lane"

#: The J7 entry of the join refusal overlay, as it stands in the overlays repository.
J7_JOIN: dict[str, Any] = {
    "join": "J7",
    "from_piece": 7,
    "to_piece": 8,
    "downstream_node": "node_lab_check_run_effect:join_j7",
    "injected_fault": "Drop the piece 8 refusal from the emission journal",
    "upstream": {
        "piece": 7,
        "node": "node_stop_drill_effect:run",
        "golden": {
            "row_type": "STATUS",
            "lane": LANE,
            "cells": {
                "src": LANE,
                "drill": "no",
                "verdict": "WORKING",
                "outcome": "read",
            },
        },
        "error": {
            "row_type": "STATUS",
            "lane": LANE,
            "cells": {"src": LANE, "verdict": "NOT_WORKING"},
        },
        "failure_code_cell": "reason",
        "failure_default": "stop_drill_untyped",
        "error_drill_cell": "drill",
        "lookback_days": 7,
    },
}


def refusal_overlay() -> ModelJoinRefusalOverlay:
    document = yaml.safe_load(
        (NODES / "node_join_refusal_effect/overlay.example.yaml").read_text(
            encoding="utf-8"
        )
    )
    document["joins"] = [J7_JOIN]
    return ModelJoinRefusalOverlay.model_validate(document)


def drill_overlay() -> ModelStopDrillOverlay:
    document = yaml.safe_load(
        (NODES / "node_stop_drill_effect/overlay.example.yaml").read_text(
            encoding="utf-8"
        )
    )
    document["emission"]["ledger"]["lane"] = LANE
    return ModelStopDrillOverlay.model_validate(document)


def drill_row(
    verdict: str = "NOT_WORKING",
    *,
    drill: bool = False,
    reason: str = REASON,
    at: datetime = FAILED_AT,
) -> str:
    """One row from the real stop drill's writer, one line without the line end."""
    return ledger_row(
        drill_overlay(),
        run_id=f"run-{uuid.uuid4().hex[:8]}",
        host="host-a",
        verdict=verdict,
        drill=drill,
        reason=reason,
        probed_at=at,
        published_at=at,
    ).rstrip("\n")


def renamed(row: str, cell: str, new: str) -> str:
    """The planted break: one cell of the producer's row under another name."""
    assert f" | {cell}=" in row
    return row.replace(f" | {cell}=", f" | {new}=")


def ledger(tmp_path: Path, *rows: str) -> FileLedgerRowSource:
    tmp_path.mkdir(parents=True, exist_ok=True)
    live = tmp_path / "LEDGER.md"
    live.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return FileLedgerRowSource(live, None)


class Unreadable:
    def read(self, query: ModelLedgerQuery) -> ModelLedgerRead:
        return ModelLedgerRead(error="ledger-file-unreadable:OSError")


class DropsPiece(InMemoryEmissionSink):
    """The failing control: a journal that loses the downstream piece's terminal emission."""

    def __init__(self, piece: int) -> None:
        super().__init__()
        self._piece = piece

    def emit(self, event: ModelNodeEmission) -> None:
        if event.piece != self._piece:
            super().emit(event)


def answer(source: Any, sink: InMemoryEmissionSink) -> Any:
    return HandlerJoinRefusal(
        overlay=refusal_overlay(),
        source=source,
        sink=sink,
        backend="in_memory",
        clock=lambda: NOW,
    ).handle(ModelJoinRefusalCommand(write=True))


def receipts(source: Any, sink: InMemoryEmissionSink) -> ModelFloorReceipts:
    """The floor's own fold: the ledger rows as piece events, plus the journal, judged for join J7."""
    rule = refusal_overlay().joins[0].upstream
    rows = source.read(
        ModelLedgerQuery(
            since=NOW - timedelta(days=7),
            until=NOW + timedelta(days=1),
            row_type="STATUS",
            lane=LANE,
        )
    ).rows
    events = (*sink.read(NOW - timedelta(hours=48)), *piece_events(list(rows), (rule,)))
    return HandlerFloorReceipt().handle(
        ModelFloorReceiptInput(
            now=NOW,
            emissions=events,
            pieces=tuple(
                ModelPieceDef(piece=n, name=f"piece {n}", reading_max_age_s=86400)
                for n in range(1, 19)
            ),
            joins=(
                ModelJoinDef(id="J7", from_piece=7, to_piece=8, label="spine to CI"),
            ),
            freshness_max_age_s=7200,
            error_max_age_s=86400,
            chain_deadline_s=DEADLINE_S,
            judged_to_pieces=(7, 8),
        )
    )


def codes(found: ModelFloorReceipts) -> list[str]:
    return [f.code for f in found.findings]


def golden_expectation(tmp_path: Path, row: str) -> None:
    """The seam's golden reading: the failure of piece 7 is answered once and the join reads as an error join."""
    source = ledger(tmp_path, row)
    sink = InMemoryEmissionSink()
    result = answer(source, sink)
    assert result.ok
    [reading] = result.readings
    assert (reading.status, reading.owed, reading.written) == ("owed", 1, 1)
    [refusal] = sink.events
    assert (refusal.piece, refusal.phase) == (8, "failure")
    assert refusal.failure_code == f"upstream_{REASON}"
    found = receipts(source, sink)
    assert codes(found) == []
    assert found.joins[0].state == "error_only"


# ---- golden ----


def test_golden_the_floor_reads_the_stop_drill_failure_unanswered_before_the_node_runs(
    tmp_path: Path,
) -> None:
    found = receipts(ledger(tmp_path, drill_row()), InMemoryEmissionSink())
    assert codes(found) == ["downstream_missing_typed_failure"]
    assert found.joins[0].state == "broken"


def test_golden_a_real_piece_7_failure_is_answered_with_piece_8s_typed_refusal(
    tmp_path: Path,
) -> None:
    golden_expectation(tmp_path, drill_row())


def test_golden_the_refusal_is_written_once(tmp_path: Path) -> None:
    source = ledger(tmp_path, drill_row())
    sink = InMemoryEmissionSink()
    answer(source, sink)
    second = answer(source, sink)
    assert [(r.owed, r.written) for r in second.readings] == [(0, 0)]
    assert len(sink.events) == 1


# ---- error ----


def test_error_the_planted_fault_of_the_drill_is_never_answered(
    tmp_path: Path,
) -> None:
    sink = InMemoryEmissionSink()
    result = answer(ledger(tmp_path, drill_row(drill=True)), sink)
    assert sink.events == []
    assert [(r.owed, r.written) for r in result.readings] == [(0, 0)]


def test_error_a_working_row_is_not_a_failure_and_is_not_answered(
    tmp_path: Path,
) -> None:
    sink = InMemoryEmissionSink()
    result = answer(ledger(tmp_path, drill_row("WORKING", reason="none")), sink)
    assert sink.events == []
    assert [(r.owed, r.written) for r in result.readings] == [(0, 0)]


def test_error_an_unreadable_ledger_writes_nothing_and_says_so() -> None:
    sink = InMemoryEmissionSink()
    result = answer(Unreadable(), sink)
    assert sink.events == []
    assert not result.wrote
    assert [r.status for r in result.readings] != ["owed"]


# ---- control: the seam fails when the producer's side changes shape ----


@pytest.mark.parametrize(
    ("cell", "new"),
    [("verdict", "state"), ("reason", "why")],
)
def test_control_a_planted_rename_of_a_cell_the_stop_drill_writes_turns_the_seam_red(
    tmp_path: Path, cell: str, new: str
) -> None:
    row = renamed(drill_row(), cell, new)
    with pytest.raises(AssertionError):
        golden_expectation(tmp_path, row)


def test_control_a_planted_rename_of_the_drill_cell_turns_the_error_expectation_red(
    tmp_path: Path,
) -> None:
    """The drill cell is what keeps the planted fault unanswered: renamed, the fault is answered as a real failure."""
    plain = ledger(tmp_path / "plain", drill_row(drill=True))
    sink = InMemoryEmissionSink()
    answer(plain, sink)
    assert sink.events == []
    broken = ledger(
        tmp_path / "renamed", renamed(drill_row(drill=True), "drill", "dry_run")
    )
    sink = InMemoryEmissionSink()
    answer(broken, sink)
    assert len(sink.events) == 1


def test_control_the_unrenamed_row_passes_the_same_expectation(tmp_path: Path) -> None:
    golden_expectation(tmp_path, drill_row())


def test_control_a_journal_that_drops_piece_8_leaves_join_j7_broken(
    tmp_path: Path,
) -> None:
    source = ledger(tmp_path, drill_row())
    sink = DropsPiece(8)
    answer(source, sink)
    found = receipts(source, sink)
    assert codes(found) == ["downstream_missing_typed_failure"]
    assert found.joins[0].state == "broken"


# ---- parity: the row crosses a bus and the consumer answers the same ----


def consume(payload: dict[str, Any]) -> dict[str, Any]:
    """The consumer: the row from the message into a ledger, the join refusal node over it, the fold over the journal."""
    row = payload["row"]
    directory = Path(payload["dir"])
    directory.mkdir(parents=True, exist_ok=True)
    source = ledger(directory, row)
    sink = InMemoryEmissionSink()
    answer(source, sink)
    found = receipts(source, sink)
    return {
        "id": payload["id"],
        "refusals": [[e.piece, e.phase, e.failure_code, e.node] for e in sink.events],
        "findings": codes(found),
        "state": found.joins[0].state,
    }


class Bus:
    """Carry one JSON request to the consumer and its JSON answer back."""

    async def start(self) -> None: ...

    async def call(self, payload: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def stop(self) -> None: ...


class InmemoryBus(Bus):
    def __init__(self) -> None:
        self.bus = EventBusInmemory(environment="join-j7-parity", group="parity")
        self.waiting: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self.prefix = uuid.uuid4().hex

    async def start(self) -> None:
        await self.bus.start()

        async def serve(message: Any) -> None:
            reply = consume(json.loads(message.value))
            await self.bus.publish(
                f"{self.prefix}.res", None, json.dumps(reply).encode()
            )

        async def reply_to(message: Any) -> None:
            reply = json.loads(message.value)
            self.waiting.pop(reply["id"]).set_result(reply)

        await self.bus.subscribe(
            f"{self.prefix}.req", on_message=serve, group_id="serve"
        )
        await self.bus.subscribe(
            f"{self.prefix}.res", on_message=reply_to, group_id="ask"
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
        self.prefix = f"join-j7-parity-{uuid.uuid4().hex}"
        self.tasks: list[asyncio.Task[None]] = []
        self.consumers: list[Any] = []
        self.waiting: dict[str, asyncio.Future[dict[str, Any]]] = {}

    async def start(self) -> None:
        from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

        self.producer = AIOKafkaProducer(
            bootstrap_servers=self.bootstrap, security_protocol="PLAINTEXT"
        )
        await self.producer.start()

        async def run_consumer(
            topic: str, handle: Callable[[dict[str, Any]], Any]
        ) -> None:
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
                f"{self.prefix}.res", json.dumps(consume(request)).encode()
            )

        async def reply_to(reply: dict[str, Any]) -> None:
            future = self.waiting.pop(reply["id"], None)
            if future is not None:
                future.set_result(reply)

        self.tasks.append(
            asyncio.create_task(run_consumer(f"{self.prefix}.req", serve))
        )
        self.tasks.append(
            asyncio.create_task(run_consumer(f"{self.prefix}.res", reply_to))
        )
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


def request(tmp_path: Path, row: str) -> dict[str, Any]:
    return {"id": uuid.uuid4().hex, "row": row, "dir": str(tmp_path / uuid.uuid4().hex)}


async def over(bus: Bus, tmp_path: Path, row: str) -> dict[str, Any]:
    await bus.start()
    try:
        return await bus.call(request(tmp_path, row))
    finally:
        await bus.stop()


def settled(reply: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in reply.items() if k != "id"}


EXPECTED = {
    "refusals": [
        [8, "failure", f"upstream_{REASON}", "node_lab_check_run_effect:join_j7"]
    ],
    "findings": [],
    "state": "error_only",
}


def test_parity_in_process_answer_is_the_expected_refusal(tmp_path: Path) -> None:
    assert settled(consume(request(tmp_path, drill_row()))) == EXPECTED


@pytest.mark.join_j7_parity
def test_parity_in_memory_bus_answers_what_the_in_process_read_answers(
    tmp_path: Path,
) -> None:
    reply = asyncio.run(over(InmemoryBus(), tmp_path, drill_row()))
    assert settled(reply) == EXPECTED


@pytest.mark.join_j7_parity
def test_parity_a_renamed_cell_crossing_the_in_memory_bus_is_not_answered(
    tmp_path: Path,
) -> None:
    row = renamed(drill_row(), "verdict", "state")
    reply = asyncio.run(over(InmemoryBus(), tmp_path, row))
    assert settled(reply) != EXPECTED


@pytest.mark.kafka
@pytest.mark.integration
@pytest.mark.join_j7_parity
def test_parity_redpanda_bus_answers_what_the_in_memory_bus_answers(
    tmp_path: Path,
) -> None:
    if not os.environ.get(BOOTSTRAP_ENV):
        pytest.skip(f"Kafka not reachable: {BOOTSTRAP_ENV} names no lab broker")
    row = drill_row()
    broker = asyncio.run(over(RedpandaBus(os.environ[BOOTSTRAP_ENV]), tmp_path, row))
    memory = asyncio.run(over(InmemoryBus(), tmp_path, row))
    assert settled(broker) == settled(memory) == EXPECTED
