# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Join J3, bus to overlays (plan 4.1, OMN-22910): one overlay value changed, a node triggered by a command behaves
differently with no code change, and the seam reading consumes the change; golden and error.

Piece 3 (the bus) publishes the command; piece 4 (the overlay drift read) answers it under the same correlation id; the
floor's receipt fold judges the two chains as the join and the floor's reading rule reads the row the node wrote. The
named breaks at the end each turn the join red: a node that does not emit its chain, and a node that starts although its
upstream failed.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.handlers.emission_sink import ChainEmitter, InMemoryEmissionSink
from omnimarket.models.floor_17a import (
    ModelFloorReceiptInput,
    ModelFloorReceipts,
    ModelJoinDef,
    ModelLedgerReadingRule,
    ModelPieceDef,
)
from omnimarket.nodes.node_floor_emission_compute.handlers.handler_floor_receipt import (
    HandlerFloorReceipt,
)
from omnimarket.nodes.node_floor_simple_effect.handlers.ledger_reads import (
    reading_rows,
)
from omnimarket.nodes.node_overlay_drift_effect.handlers.handler_overlay_drift import (
    HandlerOverlayDrift,
    answer_command,
    load_topics,
)
from omnimarket.nodes.node_overlay_drift_effect.overlay import (
    ModelOverlayDriftOverlay,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
    evidence_row_from_text,
)
from tests.nodes.node_overlay_drift_effect.overlay_drift_support import (
    EXAMPLE,
    RUN_AT,
    World,
)

pytestmark = pytest.mark.unit

TOPICS = load_topics()
CID = "j3-1"
BUS_NODE = "node_bus_command_publisher:publish"
J3 = ModelJoinDef(id="J3", from_piece=3, to_piece=4, label="bus to overlays")
PIECES = tuple(
    ModelPieceDef(piece=n, name=f"piece {n}", reading_max_age_s=86400) for n in (3, 4)
)
EVENT_TOPICS = [
    TOPICS[role]
    for role in (
        "overlay_drift_written",
        "overlay_drift_failed",
        "overlay_drift_run_completed",
        "overlay_drift_run_failed",
    )
]
#: The floor's rule for the drift node's own row: the row names its piece, verdict, times and how long it stays fresh.
READING_RULE = ModelLedgerReadingRule(
    row_type="STATUS",
    lane="overlay-drift",
    cells={"src": "overlay-drift"},
    piece_cell="piece",
    verdict_cell="verdict",
    probed_at_cell="probed_at",
    published_at_cell="published_at",
    max_age_cell="max_age",
    reason_cell="reason",
    lookback_days=3,
)


def overlay_with(**publish: int) -> ModelOverlayDriftOverlay:
    """The example overlay with named values of its publish block changed: the one overlay change of the join."""
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["publish"].update(publish)
    return ModelOverlayDriftOverlay.model_validate(document)


def clock() -> Callable[[], datetime]:
    """One second per call, so a start is strictly before its terminal and after the command."""
    ticks = iter(RUN_AT + timedelta(seconds=n) for n in range(1, 1000))
    return lambda: next(ticks)


class Rig:
    """Piece 3's command, the drift node on the in-memory bus, the shared emission sink and the ledger."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        overlay: ModelOverlayDriftOverlay | None = None,
        wired_sink: bool = True,
        plant_rejected_key: bool = False,
    ) -> None:
        self.sink = InMemoryEmissionSink()
        self.ledger = InMemoryLedger()
        self.tick = clock()
        world = World("memory", tmp_path)
        if plant_rejected_key:
            world.break_plant_rejected_key()
        self.handler = HandlerOverlayDrift(
            overlay=overlay or overlay_with(),
            host="example-host",
            home=world.home(),
            validator=world.validator(),
            appender=self.ledger,
            clock=self.tick,
            emissions=self.sink if wired_sink else None,
        )
        self.published: list[tuple[str, dict[str, Any]]] = []

    def bus_chain(self, *, fail: str | None = None) -> ChainEmitter:
        chain = ChainEmitter(
            self.sink,
            node=BUS_NODE,
            piece=3,
            clock=self.tick,
            correlation_id=CID,
        )
        chain.start()
        if fail:
            chain.failure(fail, "the command was not published")
        return chain

    def run(self, chain: ChainEmitter, *, upstream_failure: str | None = None) -> None:
        """Publish the command on the bus and let the node answer it.

        Piece 3's chain closes when the broker accepts the command, which is before the consumer works on it; the
        in-memory bus delivers inside ``publish``, so the success is emitted just before it.
        """
        body: dict[str, Any] = {"correlation_id": CID, "write": True}
        if upstream_failure:
            body["upstream_failure"] = upstream_failure
        self.published = asyncio.run(self._publish(json.dumps(body).encode(), chain))

    async def _publish(
        self, payload: bytes, chain: ChainEmitter
    ) -> list[tuple[str, dict[str, Any]]]:
        bus = EventBusInmemory()
        seen: list[tuple[str, dict[str, Any]]] = []
        done = asyncio.Event()

        async def on_command(message: Any) -> None:
            for topic, body in answer_command(message.value, self.handler, TOPICS):
                await bus.publish(topic, key=None, value=body)

        def collector(topic: str) -> Callable[[Any], Any]:
            async def collect(message: Any) -> None:
                seen.append((topic, json.loads(message.value)))
                if topic in (
                    TOPICS["overlay_drift_run_completed"],
                    TOPICS["overlay_drift_run_failed"],
                ):
                    done.set()

            return collect

        await bus.start()
        try:
            await bus.subscribe(
                TOPICS["overlay_drift_requested"],
                on_message=on_command,
                group_id="overlay-drift",
            )
            for topic in EVENT_TOPICS:
                await bus.subscribe(
                    topic, on_message=collector(topic), group_id=f"test-{topic}"
                )
            if chain.open:
                chain.success({"published": 1})
            await bus.publish(
                TOPICS["overlay_drift_requested"], key=None, value=payload
            )
            await asyncio.wait_for(done.wait(), timeout=5)
        finally:
            await bus.close()
        return seen

    def receipts(self) -> ModelFloorReceipts:
        return HandlerFloorReceipt().handle(
            ModelFloorReceiptInput(
                now=RUN_AT + timedelta(minutes=5),
                emissions=self.sink.read(RUN_AT - timedelta(days=1)),
                pieces=PIECES,
                joins=(J3,),
                freshness_max_age_s=900,
                error_max_age_s=86400,
                chain_deadline_s=300,
            )
        )

    def join(self) -> Any:
        (receipt,) = self.receipts().joins
        return receipt

    def piece4_failure(self) -> Any:
        (failure,) = (
            e for e in self.sink.events if e.piece == 4 and e.phase == "failure"
        )
        return failure

    def reading(self) -> Any:
        (line,) = self.ledger.lines
        row = evidence_row_from_text(line)
        assert row is not None
        readings, rejected = reading_rows([row], READING_RULE)
        assert rejected == 0
        (reading,) = readings
        return reading


# --- golden -------------------------------------------------------------------------------------------------------


def test_golden_overlay_value_changed_node_behaves_differently_and_the_seam_reading_consumes_it(
    tmp_path: Path,
) -> None:
    before = Rig(tmp_path / "before", overlay=overlay_with())
    before.run(before.bus_chain())
    after = Rig(tmp_path / "after", overlay=overlay_with(publish_max_age_seconds=3600))
    after.run(after.bus_chain())

    # No code changed between the runs: the one overlay value did. The node's reading row carries it, and the floor's
    # reading rule takes the row's own freshness bound, so the reading consumes the change.
    assert before.reading().max_age_s == 129600
    assert after.reading().max_age_s == 3600
    assert after.reading().piece == 4
    assert after.reading().state == "WORKING"
    # The join: piece 3 succeeded, piece 4 started after that and succeeded, under the command's correlation id.
    assert [t for t, _ in after.published] == [
        TOPICS["overlay_drift_written"],
        TOPICS["overlay_drift_run_completed"],
    ]
    assert {body["correlation_id"] for _, body in after.published} == {CID}
    receipt = after.join()
    assert receipt.golden_at is not None
    assert receipt.state == "golden_only"
    assert after.receipts().findings == ()


# --- error --------------------------------------------------------------------------------------------------------


def test_error_overlay_value_the_runtime_rejects_is_a_typed_failure_naming_the_key_and_reads_not_working(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path, plant_rejected_key=True)
    rig.run(rig.bus_chain())

    failure = rig.piece4_failure()
    assert failure.failure_code == "overlay_not_accepted"
    assert failure.correlation_id == CID
    # The key is named, with its overlay; the planted value is never carried.
    assert "node_reading_writer_effect:rejected:rung_order" in failure.failure_detail
    assert "opus" not in failure.failure_detail
    assert "claude" not in failure.failure_detail
    reading = rig.reading()
    assert reading.state == "NOT WORKING"
    assert reading.piece == 4
    assert "rejected-1" in reading.reason
    # The fault is piece 4's own, so the join has no golden receipt and is not a break of the join.
    receipt = rig.join()
    assert receipt.golden_at is None
    assert receipt.state != "broken"


def test_error_upstream_failure_gives_a_typed_refusal_and_the_node_does_not_start(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path)
    rig.run(rig.bus_chain(fail="publish_refused"), upstream_failure="publish_refused")

    refusal = rig.piece4_failure()
    assert refusal.failure_code == "upstream_publish_refused"
    assert not [e for e in rig.sink.events if e.piece == 4 and e.phase == "start"]
    assert rig.ledger.lines == []
    assert [t for t, _ in rig.published] == [TOPICS["overlay_drift_run_failed"]]
    receipt = rig.join()
    assert receipt.error_at is not None
    assert receipt.state == "error_only"
    assert rig.receipts().findings == ()


# --- the named breaks: each turns the join red --------------------------------------------------------------------


def test_break_node_that_does_not_emit_its_chain_leaves_the_join_unproven(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path, wired_sink=False)
    rig.run(rig.bus_chain())
    receipt = rig.join()
    assert receipt.golden_at is None
    assert receipt.state == "not_run"


def test_break_node_that_starts_although_its_upstream_failed_reads_the_join_broken(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path)
    rig.run(
        rig.bus_chain(fail="publish_refused")
    )  # the command carries no upstream failure
    receipt = rig.join()
    assert receipt.state == "broken"
    assert [f.code for f in rig.receipts().findings] == [
        "downstream_started_after_upstream_failed"
    ]
