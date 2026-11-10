# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seam of join J6 (piece 6 to piece 7): the failure the J5 join handler writes to the emission journal is the chain the J6 refusal rule answers (OMN-23421).

The producer side is the real J5 join handler (``HandlerWorkLedgerJoinJ5``) over the J5 policy from
``load_join_j5_policy()``: when the durable row lands but the projection never hears it, the handler writes a start and a
typed failure ``projected_row_missing`` to the emission journal under the correlation id. The consumer side is the join
refusal node over the J6 entry of the join refusal overlay, which names that producer node in its ``journal_upstream``
rule, and the floor's own receipt fold judges the join. Neither side is stubbed: the test changes the shape of the
producer's chain and reads what the consumer does.

* golden: the producer's failure chain is answered once with piece 7's typed ``upstream_projected_row_missing`` refusal
  and the fold reads J6 as an error join with no finding (before the node runs it reads
  ``downstream_missing_typed_failure``); the same answer is written by the real JsonlEmissionSink on a file.
* error: a drill chain of the downstream node, the identity-unresolved refusal chain (a typed failure with no start), a
  successful J5 chain and an unreadable ledger are never answered.
* control: an overlay without the journal rule leaves the producer's chain unanswered and the fold still reads the
  finding; a journal that drops piece 7 leaves J6 broken; a planted rename of the producer's node turns the seam red.
* parity: the same request crosses a bus (in-memory always, Redpanda when ``JOIN_J6_PARITY_BOOTSTRAP`` names a lab broker)
  and the consumer answers the same refusal as an in-process read.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.handlers.emission_sink import (
    ChainEmitter,
    InMemoryEmissionSink,
    JsonlEmissionSink,
    ProtocolEmissionSink,
)
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
    InMemoryLedger,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ModelLedgerQuery,
    ModelLedgerRead,
)
from tests.unit.nodes.node_work_ledger_append_effect.test_join_j5 import (
    NOW as J5_NOW,
)
from tests.unit.nodes.node_work_ledger_append_effect.test_join_j5 import (
    POLICY,
    _Clock,
    _request,
    _Rig,
)

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[3]
NODES = REPO_ROOT / "src/omnimarket/nodes"
BOOTSTRAP_ENV = "JOIN_J6_PARITY_BOOTSTRAP"
TIMEOUT_S = 30.0
DEADLINE_S = 900
#: The fold and the refusal node run an hour after the producer wrote its chain (the producer rig keeps its own clock).
NOW = J5_NOW + timedelta(hours=1)
LANE = "host-reading"
REASON = "no_durable_row_by_deadline"

#: The J6 entry of the join refusal overlay, as it stands in the overlays repository.
J6_JOIN: dict[str, Any] = {
    "join": "J6",
    "from_piece": 6,
    "to_piece": 7,
    "downstream_node": "node_gate_store_effect:join_j6",
    "injected_fault": "Drop the piece 7 refusal from the emission journal",
    "upstream": {
        "piece": 6,
        "node": "node_host_reading_effect:ledger_durability",
        "golden": {
            "row_type": "STATUS",
            "lane": LANE,
            "cells": {
                "src": LANE,
                "probe": "ledger-durability",
                "drill": "no",
                "verdict": "WORKING",
                "outcome": "read",
            },
        },
        "error": {
            "row_type": "STATUS",
            "lane": LANE,
            "cells": {
                "src": LANE,
                "probe": "ledger-durability",
                "verdict": "NOT_WORKING",
            },
        },
        "failure_code_cell": "reason",
        "failure_default": REASON,
        "error_drill_cell": "drill",
        "lookback_days": 7,
    },
    "journal_upstream": {"nodes": [POLICY.emission_node]},
}


def refusal_overlay() -> ModelJoinRefusalOverlay:
    document = yaml.safe_load(
        (NODES / "node_join_refusal_effect/overlay.example.yaml").read_text(
            encoding="utf-8"
        )
    )
    document["joins"] = [J6_JOIN]
    return ModelJoinRefusalOverlay.model_validate(document)


def control_overlay() -> ModelJoinRefusalOverlay:
    """The overlay without the journal rule: the producer's chain is no longer named."""
    join = dict(J6_JOIN)
    join.pop("journal_upstream")
    document = yaml.safe_load(
        (NODES / "node_join_refusal_effect/overlay.example.yaml").read_text(
            encoding="utf-8"
        )
    )
    document["joins"] = [join]
    return ModelJoinRefusalOverlay.model_validate(document)


def renamed_overlay() -> ModelJoinRefusalOverlay:
    """The planted break: the producer's node is renamed, so the journal rule names a node that never emits."""
    join = dict(J6_JOIN)
    join["journal_upstream"] = {"nodes": [f"{POLICY.emission_node}_renamed"]}
    document = yaml.safe_load(
        (NODES / "node_join_refusal_effect/overlay.example.yaml").read_text(
            encoding="utf-8"
        )
    )
    document["joins"] = [join]
    return ModelJoinRefusalOverlay.model_validate(document)


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


def answer(
    source: Any,
    sink: ProtocolEmissionSink,
    *,
    overlay: ModelJoinRefusalOverlay | None = None,
) -> Any:
    return HandlerJoinRefusal(
        overlay=overlay or refusal_overlay(),
        source=source,
        sink=sink,
        backend="in_memory",
        clock=lambda: NOW,
    ).handle(ModelJoinRefusalCommand(write=True))


def receipts(
    source: Any,
    sink: ProtocolEmissionSink,
    *,
    overlay: ModelJoinRefusalOverlay | None = None,
) -> ModelFloorReceipts:
    """The floor's own fold: the journal chains plus the ledger rows as piece events, judged for join J6."""
    rule = (overlay or refusal_overlay()).joins[0].upstream
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
                ModelJoinDef(id="J6", from_piece=6, to_piece=7, label="ledger to gate"),
            ),
            freshness_max_age_s=7200,
            error_max_age_s=86400,
            chain_deadline_s=DEADLINE_S,
            judged_to_pieces=(6, 7),
        )
    )


def refusals(sink: InMemoryEmissionSink) -> list[ModelNodeEmission]:
    """What piece 7 wrote: the producer's own piece 6 chain is in the same journal and is not an answer."""
    return [e for e in sink.events if e.piece == 7]


def codes(found: ModelFloorReceipts) -> list[str]:
    return [f.code for f in found.findings]


def producer_failure_chain() -> tuple[str, InMemoryEmissionSink]:
    """One run of the real J5 join handler: the durable row lands, the projection never hears it, and the handler writes
    a start then a typed failure ``projected_row_missing`` to the emission journal."""
    rig = _Rig(projection_hears_ledger=False)
    cid = uuid.uuid4().hex
    rig.upstream_chain(cid, resolves=True)
    rig.join.handle(_request(cid))
    return cid, rig.sink


def producer_success_chain() -> tuple[str, InMemoryEmissionSink]:
    """One run of the real J5 join handler: the projection hears the row and the handler writes a start then a success."""
    rig = _Rig()
    cid = uuid.uuid4().hex
    rig.upstream_chain(cid, resolves=True)
    rig.join.handle(_request(cid))
    return cid, rig.sink


def producer_identity_unresolved_chain() -> tuple[str, InMemoryEmissionSink]:
    """One run of the real J5 join handler: the store has no such identity, and the handler writes a typed failure with no
    start and code ``upstream_identity_key_unresolved``."""
    rig = _Rig(identities={})
    cid = uuid.uuid4().hex
    rig.upstream_chain(cid, resolves=False)
    rig.join.handle(_request(cid))
    return cid, rig.sink


# ---- golden ----


def test_golden_the_floor_reads_the_producer_failure_unanswered_before_the_node_runs() -> (
    None
):
    _, sink = producer_failure_chain()
    found = receipts(InMemoryLedger([]), sink)
    assert codes(found) == ["downstream_missing_typed_failure"]
    assert found.joins[0].state == "broken"


def test_golden_a_real_piece_6_failure_is_answered_with_piece_7s_typed_refusal() -> (
    None
):
    cid, sink = producer_failure_chain()
    source = InMemoryLedger([])
    result = answer(source, sink)
    assert result.ok
    [reading] = result.readings
    assert (reading.status, reading.owed, reading.written) == ("owed", 1, 1)
    [refusal] = refusals(sink)
    assert (refusal.piece, refusal.phase) == (7, "failure")
    assert refusal.failure_code == "upstream_projected_row_missing"
    assert refusal.node == "node_gate_store_effect:join_j6"
    assert refusal.correlation_id == cid
    found = receipts(source, sink)
    assert codes(found) == []
    assert found.joins[0].state == "error_only"


def test_golden_the_refusal_is_written_once() -> None:
    _, sink = producer_failure_chain()
    source = InMemoryLedger([])
    answer(source, sink)
    second = answer(source, sink)
    assert [(r.owed, r.written) for r in second.readings] == [(0, 0)]
    assert len(refusals(sink)) == 1


def test_golden_the_real_journal_backend_gives_the_same_answer(tmp_path: Path) -> None:
    cid, memory = producer_failure_chain()
    journal = JsonlEmissionSink(tmp_path / "journal" / "emissions.jsonl")
    for event in memory.events:
        journal.emit(event)
    source = InMemoryLedger([])
    result = answer(source, journal)
    assert result.ok
    [reading] = result.readings
    assert (reading.status, reading.owed, reading.written) == ("owed", 1, 1)
    [refusal] = [e for e in journal.read(NOW - timedelta(hours=48)) if e.piece == 7]
    assert (refusal.piece, refusal.phase) == (7, "failure")
    assert refusal.failure_code == "upstream_projected_row_missing"
    assert refusal.node == "node_gate_store_effect:join_j6"
    assert refusal.correlation_id == cid
    found = receipts(source, journal)
    assert codes(found) == []
    assert found.joins[0].state == "error_only"


def test_overlay_rule_names_the_piece_6_chain_node_and_never_the_refusal_node() -> None:
    """AC1 on the hosted side: the J6 entry's journal rule names the J5 handler's own chain node, not piece 7's refusal."""
    join = refusal_overlay().joins[0]
    assert join.journal_upstream is not None
    assert list(join.journal_upstream.nodes) == [POLICY.emission_node]
    assert join.downstream_node not in join.journal_upstream.nodes
    assert not any(node.endswith(":join_j6") for node in join.journal_upstream.nodes)


# ---- error ----


def test_error_a_drill_chain_of_the_producer_node_is_never_answered() -> None:
    """A drill is not evidence for a join: the producer's own planted failure is counted as skipped and writes nothing."""
    sink = InMemoryEmissionSink()
    chain = ChainEmitter(
        sink,
        node=POLICY.emission_node,
        piece=POLICY.to_piece,
        clock=_Clock(),
        correlation_id=uuid.uuid4().hex,
        backend="in_memory",
        drill=True,
    )
    chain.start()
    chain.failure(POLICY.projected_row_missing_failure_code, "planted fault")
    result = answer(InMemoryLedger([]), sink)
    assert refusals(sink) == []
    assert [(r.owed, r.written, r.drills_skipped) for r in result.readings] == [
        (0, 0, 1)
    ]


def test_error_the_identity_unresolved_refusal_chain_is_never_answered() -> None:
    _, sink = producer_identity_unresolved_chain()
    result = answer(InMemoryLedger([]), sink)
    assert refusals(sink) == []
    assert [(r.owed, r.written) for r in result.readings] == [(0, 0)]


def test_error_a_successful_j5_chain_is_never_answered() -> None:
    _, sink = producer_success_chain()
    result = answer(InMemoryLedger([]), sink)
    assert refusals(sink) == []
    assert [(r.owed, r.written) for r in result.readings] == [(0, 0)]


def test_error_an_unreadable_ledger_writes_nothing_and_says_so() -> None:
    sink = InMemoryEmissionSink()
    result = answer(Unreadable(), sink)
    assert refusals(sink) == []
    assert not result.wrote
    assert [r.status for r in result.readings] != ["owed"]


# ---- control: the seam fails when the producer's side changes shape ----


def test_control_an_overlay_without_the_journal_rule_leaves_the_producer_failure_unanswered() -> (
    None
):
    _, sink = producer_failure_chain()
    source = InMemoryLedger([])
    result = answer(source, sink, overlay=control_overlay())
    assert refusals(sink) == []
    assert [(r.owed, r.written) for r in result.readings] == [(0, 0)]
    found = receipts(source, sink, overlay=control_overlay())
    assert codes(found) == ["downstream_missing_typed_failure"]
    assert found.joins[0].state == "broken"


def test_control_a_journal_that_drops_piece_7_leaves_join_j6_broken() -> None:
    _, produced = producer_failure_chain()
    sink = DropsPiece(7)
    for event in produced.events:
        sink.emit(event)
    source = InMemoryLedger([])
    answer(source, sink)
    assert refusals(sink) == []
    found = receipts(source, sink)
    assert codes(found) == ["downstream_missing_typed_failure"]
    assert found.joins[0].state == "broken"


def test_control_a_planted_rename_of_the_producer_node_turns_the_seam_red() -> None:
    _, sink = producer_failure_chain()
    source = InMemoryLedger([])
    result = answer(source, sink, overlay=renamed_overlay())
    assert refusals(sink) == []
    assert [(r.owed, r.written) for r in result.readings] == [(0, 0)]
    found = receipts(source, sink, overlay=renamed_overlay())
    assert codes(found) == ["downstream_missing_typed_failure"]
    assert found.joins[0].state == "broken"


# ---- parity: the request crosses a bus and the consumer answers the same ----


def consume(payload: dict[str, Any]) -> dict[str, Any]:
    """The consumer: the real J5 handler's failure chain, the join refusal node over it, the fold over the journal."""
    _, sink = producer_failure_chain()
    source = InMemoryLedger([])
    answer(source, sink)
    found = receipts(source, sink)
    return {
        "id": payload["id"],
        "refusals": [
            [e.piece, e.phase, e.failure_code, e.node] for e in refusals(sink)
        ],
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
        self.bus = EventBusInmemory(environment="join-j6-parity", group="parity")
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
        self.prefix = f"join-j6-parity-{uuid.uuid4().hex}"
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


def request() -> dict[str, Any]:
    return {"id": uuid.uuid4().hex}


async def over(bus: Bus) -> dict[str, Any]:
    await bus.start()
    try:
        return await bus.call(request())
    finally:
        await bus.stop()


def settled(reply: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in reply.items() if k != "id"}


EXPECTED = {
    "refusals": [
        [
            7,
            "failure",
            "upstream_projected_row_missing",
            "node_gate_store_effect:join_j6",
        ]
    ],
    "findings": [],
    "state": "error_only",
}


def test_parity_in_process_answer_is_the_expected_refusal() -> None:
    assert settled(consume(request())) == EXPECTED


@pytest.mark.join_j6_parity
def test_parity_in_memory_bus_answers_what_the_in_process_read_answers() -> None:
    assert settled(asyncio.run(over(InmemoryBus()))) == EXPECTED


@pytest.mark.join_j6_parity
def test_parity_a_consumer_over_an_overlay_without_the_journal_rule_does_not_answer() -> (
    None
):
    """The failing control of the parity leg: the same request, a consumer whose overlay lacks the rule, answers nothing."""
    _, sink = producer_failure_chain()
    answer(InMemoryLedger([]), sink, overlay=control_overlay())
    assert refusals(sink) == []


@pytest.mark.kafka
@pytest.mark.integration
@pytest.mark.join_j6_parity
def test_parity_redpanda_bus_answers_what_the_in_memory_bus_answers() -> None:
    if not os.environ.get(BOOTSTRAP_ENV):
        pytest.skip(f"Kafka not reachable: {BOOTSTRAP_ENV} names no lab broker")
    broker = asyncio.run(over(RedpandaBus(os.environ[BOOTSTRAP_ENV])))
    memory = asyncio.run(over(InmemoryBus()))
    assert settled(broker) == settled(memory) == EXPECTED
