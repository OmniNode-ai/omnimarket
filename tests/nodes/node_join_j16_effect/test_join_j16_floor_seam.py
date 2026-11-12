# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seam of join J16 (piece 16 to piece 17): the refusal the J16 node writes for piece 17 is a chain the floor reads (OMN-23601).

The floor judges a join into piece 17 on the chains of the nodes the floor simple contract lists in ``pipeline_chains``
and on no other: a typed refusal written under a node that is not listed reads as no answer, and 900 s after piece 16
failed the join reads ``downstream_missing_typed_failure``. The J16 unit tests fold the journal with no declared chains
and so never saw it.

The producer side is the real J16 handler (``HandlerJoinJ16``) over the example overlay, which names the piece 17 node
the contract lists. The consumer side is the floor's own receipt fold, given the production declared chains
(``load_runtime().pipeline_chains``). Neither side is stubbed: the test changes the shape of one and reads the other.

* golden: a piece 16 reading with no result is answered once with piece 17's typed ``upstream_`` refusal and the fold
  reads J16 ``error_only`` with no finding; a fresh result reads ``golden_only``; the real JSONL journal gives the same
  answer; the overlay's piece 17 node is among the contract's chains.
* error: a piece 17 node the floor does not read is refused by name (and the step exits 3); a stale and an absent
  reading are answered the same way as an unknown one.
* control: the contract entry dropped, the piece 17 node renamed and a journal that drops piece 17 each leave J16
  broken (the golden assertions fail without the change).
* parity: the same request crosses a bus (in-memory always, a Redpanda broker when ``JOIN_J16_PARITY_BOOTSTRAP`` names
  one) and the consumer answers what an in-process run answers.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.handlers.emission_sink import (
    InMemoryEmissionSink,
    JsonlEmissionSink,
)
from omnimarket.models.floor_17a import (
    ModelFloorReceiptInput,
    ModelFloorReceipts,
    ModelJoinDef,
    ModelNodeEmission,
    ModelPieceChains,
    ModelPieceDef,
)
from omnimarket.nodes.node_floor_emission_compute.handlers.handler_floor_receipt import (
    HandlerFloorReceipt,
)
from omnimarket.nodes.node_floor_simple_effect.handlers.handler_floor_simple_tick import (
    load_runtime,
)
from omnimarket.nodes.node_join_j16_effect import HandlerJoinJ16
from omnimarket.nodes.node_join_j16_effect.handlers.handler_join_j16 import (
    FAIL_NO_READING,
    FAIL_NO_RESULT,
    FAIL_STALE,
    refuse_unread_floor_node,
)
from omnimarket.nodes.node_join_j16_effect.models import ModelJoinJ16Command
from omnimarket.nodes.node_join_j16_effect.overlay import ModelJoinJ16Overlay
from omnimarket.nodes.node_join_j16_effect.runtime_join_j16 import main
from tests.unit.nodes.node_join_j16_effect.test_join_j16 import (
    EXAMPLE,
    NOW,
    _drawn,
    _ledger,
    _reading,
)

pytestmark = [pytest.mark.unit]

BOOTSTRAP_ENV = "JOIN_J16_PARITY_BOOTSTRAP"
TIMEOUT_S = 30.0
DEADLINE_S = 900
PIPELINE = load_runtime().pipeline_chains
EXAMPLE_DOCUMENT = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
FLOOR_NODE = EXAMPLE_DOCUMENT["join"]["to_node"]


def overlay(to_node: str = FLOOR_NODE) -> ModelJoinJ16Overlay:
    document = yaml.safe_load(yaml.safe_dump(EXAMPLE_DOCUMENT))
    document["join"]["to_node"] = to_node
    return ModelJoinJ16Overlay.model_validate(document)


class DropsPiece(InMemoryEmissionSink):
    """The failing control: a journal that loses the downstream piece's emissions."""

    def __init__(self, piece: int) -> None:
        super().__init__()
        self._piece = piece

    def emit(self, event: ModelNodeEmission) -> None:
        if event.piece != self._piece:
            super().emit(event)


def produce(
    tmp_path: Path,
    row: str | None,
    *,
    sink: InMemoryEmissionSink | None = None,
    to_node: str = FLOOR_NODE,
) -> InMemoryEmissionSink:
    """One run of the real J16 handler over a ledger holding at most one piece 16 reading row."""
    sink = sink if sink is not None else InMemoryEmissionSink()
    HandlerJoinJ16(
        overlay=overlay(to_node),
        source=_ledger(tmp_path, *((row,) if row else ())),
        sink=sink,
        floor_record_path=_drawn(tmp_path),
        backend="in_memory",
        clock=lambda: NOW,
    ).handle(ModelJoinJ16Command(write=True))
    return sink


def fold(
    events: Sequence[ModelNodeEmission],
    pipeline_chains: Sequence[str] = PIPELINE,
) -> ModelFloorReceipts:
    """The floor's own fold, past the chain deadline, with the floor's declared chains for piece 17."""
    return HandlerFloorReceipt().handle(
        ModelFloorReceiptInput(
            now=NOW + timedelta(seconds=DEADLINE_S + 61),
            emissions=tuple(events),
            pieces=tuple(
                ModelPieceDef(piece=n, name=f"piece {n}", reading_max_age_s=86400)
                for n in range(1, 19)
            ),
            joins=(
                ModelJoinDef(
                    id="J16", from_piece=16, to_piece=17, label="evals to the floor"
                ),
            ),
            freshness_max_age_s=7200,
            error_max_age_s=86400,
            chain_deadline_s=DEADLINE_S,
            judged_to_pieces=(16, 17),
            declared_chains=(ModelPieceChains(piece=17, nodes=tuple(pipeline_chains)),),
        )
    )


def refusals(events: Sequence[ModelNodeEmission]) -> list[list[Any]]:
    """What piece 17 wrote: [piece, phase, failure code, node]."""
    return [[e.piece, e.phase, e.failure_code, e.node] for e in events if e.piece == 17]


def codes(found: ModelFloorReceipts) -> list[str]:
    return [f.code for f in found.findings]


def unknown_reading() -> str:
    return _reading(NOW - timedelta(minutes=10), "UNKNOWN")


# ---- golden ----


def test_golden_a_reading_with_no_result_is_answered_by_piece_17_and_read_by_the_floor(
    tmp_path: Path,
) -> None:
    sink = produce(tmp_path, unknown_reading())
    found = fold(sink.events)
    assert refusals(sink.events) == [
        [17, "failure", f"upstream_{FAIL_NO_RESULT}", FLOOR_NODE]
    ]
    assert len({e.correlation_id for e in sink.events}) == 1
    assert codes(found) == []
    assert found.joins[0].state == "error_only"


def test_golden_a_fresh_eval_result_reads_golden_only(tmp_path: Path) -> None:
    sink = produce(tmp_path, _reading(NOW - timedelta(minutes=10), "WORKING"))
    found = fold(sink.events)
    assert refusals(sink.events) == [
        [17, "start", None, FLOOR_NODE],
        [17, "success", None, FLOOR_NODE],
    ]
    assert codes(found) == []
    assert found.joins[0].state == "golden_only"


def test_golden_the_real_journal_backend_gives_the_same_answer(tmp_path: Path) -> None:
    journal = tmp_path / "emissions.jsonl"
    HandlerJoinJ16(
        overlay=overlay(),
        source=_ledger(tmp_path, unknown_reading()),
        sink=JsonlEmissionSink(journal),
        floor_record_path=_drawn(tmp_path),
        backend="real",
        clock=lambda: NOW,
    ).handle(ModelJoinJ16Command(write=True))
    events = JsonlEmissionSink(journal).read(datetime(2000, 1, 1, tzinfo=UTC))
    assert refusals(events) == [
        [17, "failure", f"upstream_{FAIL_NO_RESULT}", FLOOR_NODE]
    ]
    assert codes(fold(events)) == []
    assert fold(events).joins[0].state == "error_only"


def test_golden_the_overlays_piece_17_node_is_a_chain_the_floor_reads() -> None:
    assert overlay().join.to_node in PIPELINE


# ---- error ----


def test_error_a_piece_17_node_the_floor_does_not_read_is_refused_by_name() -> None:
    refuse_unread_floor_node(FLOOR_NODE, PIPELINE)
    with pytest.raises(ValueError, match="node_not_read:join_j16"):
        refuse_unread_floor_node("node_not_read:join_j16", PIPELINE)


def test_error_the_step_exits_3_when_its_piece_17_node_is_not_one_the_floor_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    roots = tmp_path / "overlays" / "node_join_j16_effect"
    roots.mkdir(parents=True)
    document = yaml.safe_load(yaml.safe_dump(EXAMPLE_DOCUMENT))
    document["join"]["to_node"] = "node_not_read:join_j16"
    (roots / "overlay.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")
    ledger = tmp_path / "LEDGER.md"
    ledger.write_text("\n", encoding="utf-8")
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path / "overlays"))
    monkeypatch.setenv("EXAMPLE_FLOOR_HOME", str(tmp_path))
    monkeypatch.setenv("EXAMPLE_LEDGER_PATH", str(ledger))
    assert main(["run"]) == 3
    err = capsys.readouterr().err
    assert "INDETERMINATE" in err
    assert "node_not_read:join_j16" in err


@pytest.mark.parametrize(
    ("row", "code"),
    [
        (None, FAIL_NO_READING),
        (_reading(NOW - timedelta(hours=3), "WORKING"), FAIL_STALE),
    ],
)
def test_error_an_absent_and_a_stale_reading_are_answered_and_read(
    tmp_path: Path, row: str | None, code: str
) -> None:
    sink = produce(tmp_path, row)
    found = fold(sink.events)
    assert refusals(sink.events) == [[17, "failure", f"upstream_{code}", FLOOR_NODE]]
    assert codes(found) == []
    assert found.joins[0].state == "error_only"


# ---- control ----


def test_control_without_the_contract_entry_the_refusal_is_unread_and_j16_is_broken(
    tmp_path: Path,
) -> None:
    sink = produce(tmp_path, unknown_reading())
    found = fold(sink.events, [n for n in PIPELINE if n != FLOOR_NODE])
    assert "downstream_missing_typed_failure" in codes(found)
    assert found.joins[0].state == "broken"


def test_control_a_planted_rename_of_the_piece_17_node_leaves_j16_broken_and_is_refused(
    tmp_path: Path,
) -> None:
    renamed = f"{FLOOR_NODE}_renamed"
    sink = produce(tmp_path, unknown_reading(), to_node=renamed)
    found = fold(sink.events)
    assert "downstream_missing_typed_failure" in codes(found)
    assert found.joins[0].state == "broken"
    with pytest.raises(ValueError, match=renamed):
        refuse_unread_floor_node(renamed, PIPELINE)


def test_control_a_journal_that_drops_piece_17_leaves_j16_broken(
    tmp_path: Path,
) -> None:
    sink = produce(tmp_path, unknown_reading(), sink=DropsPiece(17))
    found = fold(sink.events)
    assert "downstream_missing_typed_failure" in codes(found)
    assert found.joins[0].state == "broken"


# ---- parity: the request crosses a bus and the consumer answers the same ----


def consume(payload: dict[str, Any]) -> dict[str, Any]:
    """The consumer: the real J16 handler over a reading with no result, then the floor's fold over the journal."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        sink = produce(Path(tmp), unknown_reading())
    found = fold(sink.events)
    return {
        "id": payload["id"],
        "refusals": refusals(sink.events),
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
        self.bus = EventBusInmemory(environment="join-j16-parity", group="parity")
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
        self.prefix = f"join-j16-parity-{uuid.uuid4().hex}"
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
    "refusals": [[17, "failure", f"upstream_{FAIL_NO_RESULT}", FLOOR_NODE]],
    "findings": [],
    "state": "error_only",
}


def test_parity_in_process_answer_is_the_expected_refusal() -> None:
    assert settled(consume(request())) == EXPECTED


def test_parity_in_memory_bus_answers_what_the_in_process_read_answers() -> None:
    assert settled(asyncio.run(over(InmemoryBus()))) == EXPECTED


if os.environ.get(BOOTSTRAP_ENV):

    @pytest.mark.kafka
    @pytest.mark.integration
    def test_parity_redpanda_bus_answers_what_the_in_memory_bus_answers() -> None:
        broker = asyncio.run(over(RedpandaBus(os.environ[BOOTSTRAP_ENV])))
        memory = asyncio.run(over(InmemoryBus()))
        assert settled(broker) == settled(memory) == EXPECTED
