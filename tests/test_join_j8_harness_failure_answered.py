# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Piece 9 answers every failed run of the central test harness, from the ledger and from the journal (join J8, OMN-23087).

The harness (piece 8) writes each failed run twice: a STATUS ledger row, whose chain id on the floor is the hash of the row
(never an id the harness chose), and a chain of its own in the emission journal under the run id. On the served floor the
two read ``downstream_missing_typed_failure`` after the chain deadline unless piece 9 answers each with a typed refusal: the
four chains that a lane took for control chains of the live J8 tick were such rows' hashes, and the run id chain of a real run
of 2026-11-09 19:18Z was a journal chain that no refusal rule named.

Golden: a real failed run, written by the real harness handler, is answered under both ids by the real refusal node, and the
floor's own receipt fold then reads J8 as an error join with no finding, from the in-memory sink and from the journal file.
Error: the planted failure's drill chain, a good run and an unreadable ledger are not answered.
Failing control: with the journal rule removed from the overlay the run id chain reads broken again with the finding the board
shows; with the piece 9 refusal dropped from the journal both chains do.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.emission_sink import (
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
    ModelPieceEventRule,
)
from omnimarket.nodes.node_floor_emission_compute.handlers.handler_floor_receipt import (
    HandlerFloorReceipt,
)
from omnimarket.nodes.node_floor_simple_effect.handlers.ledger_reads import (
    piece_events,
)
from omnimarket.nodes.node_join_refusal_effect import HandlerJoinRefusal
from omnimarket.nodes.node_join_refusal_effect.models import ModelJoinRefusalCommand
from omnimarket.nodes.node_join_refusal_effect.overlay import (
    ModelJoinRefusalOverlay,
)
from omnimarket.nodes.node_lab_test_harness_effect.handlers.handler_lab_test_harness_effect import (
    FAILURE_GOLDEN,
    FAILURE_PLANTED,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    FileLedgerRowSource,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ModelLedgerQuery,
    ModelLedgerRead,
)
from tests.nodes.node_floor_simple_effect.support import NOW
from tests.test_chain_events_lab_test_harness import (
    CHAIN_NODE,
    PIECE_8_RULE,
    RecordingAppender,
    run_harness,
)

pytestmark = pytest.mark.unit

RUN_ID = "run-j8-failed"
DOWNSTREAM_NODE = "node_pr_landing_decision_compute"
JUDGED_AT = NOW + timedelta(hours=1)
OVERLAY_EXAMPLE = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_join_refusal_effect/overlay.example.yaml"
)


def _overlay(
    *, journal_nodes: tuple[str, ...] | None = (CHAIN_NODE,)
) -> ModelJoinRefusalOverlay:
    """The example overlay with one join, J8 (piece 8 to piece 9), built the way the live overlay declares it."""
    data = yaml.safe_load(OVERLAY_EXAMPLE.read_text(encoding="utf-8"))
    join: dict[str, object] = {
        "join": "J8",
        "from_piece": 8,
        "to_piece": 9,
        "downstream_node": DOWNSTREAM_NODE,
        "injected_fault": "Drop the piece 9 refusal from the emission journal",
        "upstream": yaml.safe_load(PIECE_8_RULE)[0],
    }
    if journal_nodes is not None:
        join["journal_upstream"] = {"nodes": list(journal_nodes)}
    data["joins"] = [join]
    return ModelJoinRefusalOverlay.model_validate(data)


@pytest.fixture(autouse=True)
def _host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ONEX_TEST_HOST", "h053")


class _Unreadable:
    def read(self, query: ModelLedgerQuery) -> ModelLedgerRead:
        return ModelLedgerRead(error="ledger-file-unreadable:OSError")


class _DropsPiece(InMemoryEmissionSink):
    """The failing control: a journal that loses the downstream piece's terminal emission."""

    def __init__(self, piece: int) -> None:
        super().__init__()
        self._piece = piece

    def emit(self, event: ModelNodeEmission) -> None:
        if event.piece != self._piece:
            super().emit(event)


async def _harness_run(
    tmp_path: Path, *, fails: bool = True
) -> tuple[InMemoryEmissionSink, FileLedgerRowSource]:
    """One run of the real harness handler: its journal chains and the ledger rows it appended, as a ledger file."""
    sink = InMemoryEmissionSink()
    appender = RecordingAppender()
    kw = {"golden": "fails.py"} if fails else {}
    await run_harness(tmp_path, sink, RUN_ID, appender, **kw)
    live = tmp_path / "LEDGER.md"
    live.write_text("\n".join(appender.rows) + "\n", encoding="utf-8")
    return sink, FileLedgerRowSource(live, None)


def _handler(
    sink: ProtocolEmissionSink,
    source: FileLedgerRowSource | _Unreadable,
    *,
    overlay: ModelJoinRefusalOverlay | None = None,
) -> HandlerJoinRefusal:
    return HandlerJoinRefusal(
        overlay=overlay or _overlay(),
        source=source,
        sink=sink,
        backend="in_memory",
        clock=lambda: JUDGED_AT,
    )


def _fold(
    sink: ProtocolEmissionSink, source: FileLedgerRowSource
) -> ModelFloorReceipts:
    """The floor's receipt fold judged for join J8 over the journal and the ledger chains the floor builds from the rows."""
    return HandlerFloorReceipt().handle(
        ModelFloorReceiptInput(
            now=JUDGED_AT,
            emissions=(*sink.read(NOW - timedelta(days=2)), *_fold_inputs(source)),
            pieces=tuple(
                ModelPieceDef(piece=n, name=f"piece {n}", reading_max_age_s=86400)
                for n in range(1, 19)
            ),
            joins=(
                ModelJoinDef(id="J8", from_piece=8, to_piece=9, label="CI to landing"),
            ),
            freshness_max_age_s=86400,
            error_max_age_s=86400,
            chain_deadline_s=900,
            judged_to_pieces=(8, 9),
        )
    )


def _row_chain_id(source: FileLedgerRowSource) -> str:
    """The id the floor gives the ledger row's chain: the one id of a real (not drill) piece 8 chain that is not the run id."""
    ids = {
        e.correlation_id
        for e in _fold_inputs(source)
        if e.piece == 8 and not e.drill and e.correlation_id != RUN_ID
    }
    assert len(ids) == 1
    return ids.pop()


def _fold_inputs(source: FileLedgerRowSource) -> tuple[ModelNodeEmission, ...]:
    """The chains the floor builds from the harness's ledger rows (its piece 8 event rule)."""
    rule = ModelPieceEventRule.model_validate(yaml.safe_load(PIECE_8_RULE)[0])
    read = source.read(
        ModelLedgerQuery(
            since=NOW - timedelta(days=7),
            until=NOW + timedelta(days=1),
            row_type="STATUS",
            lane="central-test-harness",
        )
    )
    assert read.error is None
    return piece_events(list(read.rows), (rule,))


def _refusals(sink: InMemoryEmissionSink) -> list[ModelNodeEmission]:
    return [e for e in sink.events if e.piece == 9]


class TestGoldenChain:
    async def test_the_floor_reads_both_failure_chains_unanswered(
        self, tmp_path: Path
    ) -> None:
        """Baseline: the board's own two findings for one failed run, before the refusal node runs."""
        sink, source = await _harness_run(tmp_path)
        receipts = _fold(sink, source)
        assert sorted(f.correlation_id for f in receipts.findings) == sorted(
            [RUN_ID, _row_chain_id(source)]
        )
        assert {f.code for f in receipts.findings} == {
            "downstream_missing_typed_failure"
        }
        assert receipts.joins[0].state == "broken"

    async def test_a_failed_run_is_answered_under_both_ids_and_j8_reads_as_an_error_join(
        self, tmp_path: Path
    ) -> None:
        sink, source = await _harness_run(tmp_path)
        result = _handler(sink, source).handle(ModelJoinRefusalCommand(write=True))
        assert result.ok
        assert result.wrote
        refusals = _refusals(sink)
        assert {e.correlation_id for e in refusals} == {RUN_ID, _row_chain_id(source)}
        assert {e.node for e in refusals} == {DOWNSTREAM_NODE}
        assert {e.phase for e in refusals} == {"failure"}
        assert {e.failure_code for e in refusals} == {f"upstream_{FAILURE_GOLDEN}"}
        receipts = _fold(sink, source)
        assert receipts.findings == ()
        assert receipts.joins[0].state == "error_only"

    async def test_a_second_run_of_the_node_writes_nothing(
        self, tmp_path: Path
    ) -> None:
        sink, source = await _harness_run(tmp_path)
        handler = _handler(sink, source)
        handler.handle(ModelJoinRefusalCommand(write=True))
        again = handler.handle(ModelJoinRefusalCommand(write=True))
        assert not again.wrote
        assert len(_refusals(sink)) == 2
        assert (again.readings[0].status, again.readings[0].answered) == ("answered", 2)

    async def test_the_real_journal_backend_gives_the_same_answer(
        self, tmp_path: Path
    ) -> None:
        memory, source = await _harness_run(tmp_path)
        journal = JsonlEmissionSink(tmp_path / "journal" / "emissions.jsonl")
        for event in memory.events:
            journal.emit(event)
        _handler(journal, source).handle(ModelJoinRefusalCommand(write=True))
        receipts = _fold(journal, source)
        assert receipts.findings == ()
        assert receipts.joins[0].state == "error_only"


class TestErrorChain:
    async def test_the_planted_failure_drill_chain_is_not_answered(
        self, tmp_path: Path
    ) -> None:
        sink, source = await _harness_run(tmp_path)
        planted = [e for e in sink.events if e.node == CHAIN_NODE + ":planted_failure"]
        assert planted
        assert all(e.drill for e in planted)
        _handler(sink, source).handle(ModelJoinRefusalCommand(write=True))
        refusals = _refusals(sink)
        assert len(refusals) == 2
        assert {e.node for e in refusals} == {DOWNSTREAM_NODE}
        assert f"upstream_{FAILURE_PLANTED}" not in {e.failure_code for e in refusals}

    async def test_a_good_run_is_not_answered(self, tmp_path: Path) -> None:
        sink, source = await _harness_run(tmp_path, fails=False)
        result = _handler(sink, source).handle(ModelJoinRefusalCommand(write=True))
        assert not result.wrote
        assert _refusals(sink) == []
        assert _fold(sink, source).findings == ()

    async def test_an_unreadable_ledger_is_unread_and_writes_nothing(
        self, tmp_path: Path
    ) -> None:
        sink, _ = await _harness_run(tmp_path)
        result = _handler(sink, _Unreadable()).handle(
            ModelJoinRefusalCommand(write=True)
        )
        assert not result.ok
        assert not result.wrote
        assert _refusals(sink) == []
        assert result.readings[0].status == "unread"


class TestFailingControl:
    async def test_without_the_journal_rule_the_run_id_chain_reads_broken_again(
        self, tmp_path: Path
    ) -> None:
        """The finding the served floor shows for a real run: the row's chain is answered and the run id chain is not."""
        sink, source = await _harness_run(tmp_path)
        _handler(sink, source, overlay=_overlay(journal_nodes=None)).handle(
            ModelJoinRefusalCommand(write=True)
        )
        broken = _fold(sink, source)
        assert broken.joins[0].state == "broken"
        assert [(f.code, f.subject, f.correlation_id) for f in broken.findings] == [
            ("downstream_missing_typed_failure", "J8 piece 8 to 9", RUN_ID)
        ]

    async def test_with_the_piece_9_refusal_dropped_both_chains_read_broken_again(
        self, tmp_path: Path
    ) -> None:
        sink = _DropsPiece(9)
        memory, source = await _harness_run(tmp_path)
        for event in memory.events:
            sink.emit(event)
        _handler(sink, source).handle(ModelJoinRefusalCommand(write=True))
        broken = _fold(sink, source)
        assert broken.joins[0].state == "broken"
        assert sorted(f.correlation_id for f in broken.findings) == sorted(
            [RUN_ID, _row_chain_id(source)]
        )

    async def test_the_overlay_names_the_node_the_harness_writes_its_run_chain_under(
        self, tmp_path: Path
    ) -> None:
        """Seam: the harness's run chain node and the refusal overlay's journal rule are one name."""
        sink, _ = await _harness_run(tmp_path)
        written = {e.node for e in sink.events if e.piece == 8 and not e.drill}
        assert written == {CHAIN_NODE}
        join = _overlay().joins[0]
        assert join.journal_upstream is not None
        assert list(join.journal_upstream.nodes) == [CHAIN_NODE]
