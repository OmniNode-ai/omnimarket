# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Join J3, bus to overlays (OMN-23084): piece 3's failed seam reading is answered by piece 4's typed refusal, and the floor's
publish cycle runs the node that writes it.

Piece 3's failure chains are the seam-deadline readings that read NOT WORKING, under a correlation id the ledger row decides.
Join J3 wants a typed ``upstream_`` failure of piece 4 under that id and no start. Nothing ran the node that writes it, so the
floor read ``downstream_missing_typed_failure`` for the failure and the join read broken.

Golden: a real seam failure is answered once by piece 4 and the floor's fold reads J3 as an error join with no finding, and a
second run answers nothing again. Error: a drill, a success and a reading older than the lookback are not answered. Failing
control: with piece 4's terminal emission dropped the join reads broken with the finding the floor shows. The last tests hold
the wiring: the floor's publish cycle declares the step that runs this node, before the step that folds the journal.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.emission_sink import InMemoryEmissionSink
from omnimarket.models.floor_17a import (
    ModelFloorReceiptInput,
    ModelFloorReceipts,
    ModelJoinDef,
    ModelPieceDef,
)
from omnimarket.nodes.node_factory_floor_publish_effect.handlers.handler_factory_floor_publish import (
    load_runtime,
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
from omnimarket.nodes.node_reading_writer_effect.protocols import ModelLedgerQuery
from tests.unit.nodes.node_join_refusal_effect.test_join_refusal import (
    DEADLINE_S,
    FAILED_AT,
    NOW,
    OVERLAY_EXAMPLE,
    _DropsPiece,
    _ledger,
)

pytestmark = pytest.mark.unit

SEAM_LANE = "example-seam-deadline"
SEAM_NODE = "example_seam_node:read"
J3_DOWNSTREAM = "example_overlay_node:join_j3"


def _overlay() -> ModelJoinRefusalOverlay:
    """The example overlay with its one join shaped as J3: piece 3's seam-deadline rows, answered by piece 4."""
    data = yaml.safe_load(OVERLAY_EXAMPLE.read_text(encoding="utf-8"))
    join = data["joins"][0]
    join.update(
        {
            "join": "J3",
            "from_piece": 3,
            "to_piece": 4,
            "downstream_node": J3_DOWNSTREAM,
        }
    )
    join["upstream"].update(
        {
            "piece": 3,
            "node": SEAM_NODE,
            "golden": {
                "row_type": "STATUS",
                "lane": SEAM_LANE,
                "cells": {
                    "src": SEAM_LANE,
                    "drill": "no",
                    "verdict": "WORKING",
                    "outcome": "read",
                },
            },
            "error": {
                "row_type": "STATUS",
                "lane": SEAM_LANE,
                "cells": {"src": SEAM_LANE, "verdict": "NOT_WORKING"},
            },
            "failure_default": "no_result_by_deadline",
        }
    )
    return ModelJoinRefusalOverlay.model_validate(data)


def _seam_row(
    at: datetime, verdict: str, *, drill: str = "no", reason: str = "none"
) -> str:
    stamp = at.strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        f"{stamp} | STATUS | lane={SEAM_LANE} | src={SEAM_LANE} | verdict={verdict} "
        f"| outcome=read | reason={reason} | drill={drill} | probed_at={stamp}"
    )


def _handler(
    source: FileLedgerRowSource, sink: InMemoryEmissionSink
) -> HandlerJoinRefusal:
    return HandlerJoinRefusal(
        overlay=_overlay(),
        source=source,
        sink=sink,
        backend="in_memory",
        clock=lambda: NOW,
    )


def _j3_receipts(
    source: FileLedgerRowSource, sink: InMemoryEmissionSink
) -> ModelFloorReceipts:
    """The floor's own fold of the ledger rows as piece events plus the journal, judged for J3."""
    rule = _overlay().joins[0].upstream
    rows = source.read(
        ModelLedgerQuery(
            since=NOW - timedelta(days=7),
            until=NOW + timedelta(days=1),
            row_type="STATUS",
            lane=SEAM_LANE,
        )
    ).rows
    events = (
        *sink.read(NOW - timedelta(hours=48)),
        *piece_events(list(rows), (rule,)),
    )
    return HandlerFloorReceipt().handle(
        ModelFloorReceiptInput(
            now=NOW,
            emissions=events,
            pieces=tuple(
                ModelPieceDef(piece=n, name=f"piece {n}", reading_max_age_s=86400)
                for n in range(1, 19)
            ),
            joins=(
                ModelJoinDef(
                    id="J3", from_piece=3, to_piece=4, label="bus to overlays"
                ),
            ),
            freshness_max_age_s=7200,
            error_max_age_s=86400,
            chain_deadline_s=DEADLINE_S,
            judged_to_pieces=(3, 4),
        )
    )


class TestGoldenChain:
    def test_the_floor_reads_the_seam_failure_unanswered(self, tmp_path: Path) -> None:
        source = _ledger(tmp_path, _seam_row(FAILED_AT, "NOT_WORKING"))
        receipts = _j3_receipts(source, InMemoryEmissionSink())
        assert [f.code for f in receipts.findings] == [
            "downstream_missing_typed_failure"
        ]
        assert [f.subject for f in receipts.findings] == ["J3 piece 3 to 4"]
        assert receipts.joins[0].state == "broken"

    def test_the_seam_failure_is_answered_once_and_j3_reads_as_an_error_join(
        self, tmp_path: Path
    ) -> None:
        source = _ledger(
            tmp_path, _seam_row(FAILED_AT, "NOT_WORKING", reason="seam-timed-out")
        )
        sink = InMemoryEmissionSink()
        result = _handler(source, sink).handle(ModelJoinRefusalCommand(write=True))
        assert result.ok
        assert result.wrote
        [refusal] = sink.events
        assert (refusal.piece, refusal.phase) == (4, "failure")
        assert refusal.node == J3_DOWNSTREAM
        assert refusal.failure_code == "upstream_seam-timed-out"
        receipts = _j3_receipts(source, sink)
        assert receipts.findings == ()
        assert receipts.joins[0].state == "error_only"
        again = _handler(source, sink).handle(ModelJoinRefusalCommand(write=True))
        assert not again.wrote
        assert len(sink.events) == 1

    def test_a_failure_with_no_reason_cell_gets_the_overlays_default_code(
        self, tmp_path: Path
    ) -> None:
        source = _ledger(tmp_path, _seam_row(FAILED_AT, "NOT_WORKING"))
        sink = InMemoryEmissionSink()
        _handler(source, sink).handle(ModelJoinRefusalCommand(write=True))
        [refusal] = sink.events
        assert refusal.failure_code == "upstream_no_result_by_deadline"


class TestErrorChain:
    def test_a_drill_a_success_and_an_old_reading_are_not_answered(
        self, tmp_path: Path
    ) -> None:
        source = _ledger(
            tmp_path,
            _seam_row(FAILED_AT, "NOT_WORKING", drill="yes"),
            _seam_row(FAILED_AT - timedelta(minutes=5), "WORKING"),
            _seam_row(NOW - timedelta(days=30), "NOT_WORKING"),
        )
        sink = InMemoryEmissionSink()
        result = _handler(source, sink).handle(ModelJoinRefusalCommand(write=True))
        assert not result.wrote
        assert list(sink.events) == []


class TestFailingControl:
    def test_dropping_piece_4s_terminal_emission_turns_j3_broken_again(
        self, tmp_path: Path
    ) -> None:
        source = _ledger(tmp_path, _seam_row(FAILED_AT, "NOT_WORKING"))
        kept, dropped = InMemoryEmissionSink(), _DropsPiece(4)
        _handler(source, kept).handle(ModelJoinRefusalCommand(write=True))
        _handler(source, dropped).handle(ModelJoinRefusalCommand(write=True))
        assert _j3_receipts(source, kept).findings == ()
        broken = _j3_receipts(source, dropped)
        assert [f.code for f in broken.findings] == ["downstream_missing_typed_failure"]
        assert broken.joins[0].state == "broken"


class TestPublishCycleRunsTheNode:
    """The step that makes the node run on the floor's own tick, so the answer exists before the fold reads it."""

    def test_the_cycle_declares_the_join_refusal_step_that_writes(self) -> None:
        steps = {s.name: s for s in load_runtime().steps}
        step = steps["join_refusal"]
        assert step.kind == "module"
        assert step.module == "omnimarket.nodes.node_join_refusal_effect"
        assert step.args == ("run",)
        assert step.required is False

    def test_the_step_runs_before_the_step_that_folds_the_journal(self) -> None:
        names = [s.name for s in load_runtime().steps]
        assert names.index("join_refusal") < names.index("floor_simple")
        assert names.index("join_refusal") < names.index("factory_floor")

    def test_the_step_shares_the_concurrent_group_so_the_cycle_is_no_longer(
        self,
    ) -> None:
        steps = load_runtime().steps
        names = [s.name for s in steps]
        step = next(s for s in steps if s.name == "join_refusal")
        assert step.concurrent
        before = steps[names.index("join_refusal") - 1]
        assert before.concurrent
