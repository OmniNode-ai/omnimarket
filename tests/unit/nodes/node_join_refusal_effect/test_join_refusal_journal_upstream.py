# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Piece 4 answers a piece 3 failure chain that only the emission journal holds (join J3, bus to overlays).

The J2 check of piece 3 fails under a correlation id of its own and writes that chain straight to the emission journal, with
no ledger row. The refusal node read only ledger rows, so no piece 4 refusal ever answered such a chain: after the chain
deadline each one read ``downstream_missing_typed_failure`` on the floor (50 of the 78 findings of J3 on the served board of
2026-11-08T07:43Z; the other 28 are piece 3 refusals of J2, which the receipt fold already exempts).

Golden: a real piece 3 journal failure is answered once by piece 4's typed ``upstream_`` failure with no start, and the floor's
own receipt fold then reads J3 as an error join with no finding, from the in-memory sink and from the real journal file.
Error: a drill, a piece 3 refusal, a success, a node the overlay does not name and an unreadable ledger are not answered.
Failing control: the piece 4 refusal dropped from the journal turns J3 broken again with the finding the board shows.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

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
)
from omnimarket.nodes.node_floor_emission_compute.handlers.handler_floor_receipt import (
    HandlerFloorReceipt,
)
from omnimarket.nodes.node_join_refusal_effect import HandlerJoinRefusal
from omnimarket.nodes.node_join_refusal_effect.models import ModelJoinRefusalCommand
from omnimarket.nodes.node_join_refusal_effect.overlay import (
    ModelJoinRefusalOverlay,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    FileLedgerRowSource,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ModelLedgerQuery,
    ModelLedgerRead,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 8, 8, 0, tzinfo=UTC)
DEADLINE_S = 900
FAILED_AT = NOW - timedelta(hours=1)
CHECK_NODE = "example_seam_node:join_j2"
DOWNSTREAM_NODE = "example_overlay_node:join_j3"
OVERLAY_EXAMPLE = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_join_refusal_effect/overlay.example.yaml"
)


def _overlay(
    *, journal_nodes: tuple[str, ...] | None = (CHECK_NODE,)
) -> ModelJoinRefusalOverlay:
    """The example overlay with one join, J3 (piece 3 to piece 4), built the way the live overlay declares it."""
    data = yaml.safe_load(OVERLAY_EXAMPLE.read_text(encoding="utf-8"))
    upstream = {
        **data["joins"][0]["upstream"],
        "piece": 3,
        "node": "example_seam_node:read",
    }
    join: dict[str, object] = {
        "join": "J3",
        "from_piece": 3,
        "to_piece": 4,
        "downstream_node": DOWNSTREAM_NODE,
        "injected_fault": "Drop the piece 4 refusal from the emission journal",
        "upstream": upstream,
    }
    if journal_nodes is not None:
        join["journal_upstream"] = {"nodes": list(journal_nodes)}
    data["joins"] = [join]
    return ModelJoinRefusalOverlay.model_validate(data)


def _empty_ledger(tmp_path: Path) -> FileLedgerRowSource:
    live = tmp_path / "LEDGER.md"
    live.write_text("\n", encoding="utf-8")
    return FileLedgerRowSource(live, None)


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


def _emit(sink: ProtocolEmissionSink, *events: ModelNodeEmission) -> None:
    for event in events:
        sink.emit(event)


def _failed_chain(
    cid: str,
    *,
    node: str = CHECK_NODE,
    code: str = "TopicAuthorizationFailedError",
    at: datetime = FAILED_AT,
    drill: bool = False,
) -> tuple[ModelNodeEmission, ModelNodeEmission]:
    """A piece 3 chain with a start and a typed failure, as the J2 check writes it to the journal."""
    start = ModelNodeEmission(
        node=node,
        piece=3,
        correlation_id=cid,
        phase="start",
        emitted_at=at,
        drill=drill,
    )
    return start, start.model_copy(
        update={
            "phase": "failure",
            "failure_code": code,
            "emitted_at": at + timedelta(seconds=1),
        }
    )


def _refusal_chain(
    cid: str, *, code: str = "upstream_canary_untyped"
) -> ModelNodeEmission:
    """A piece 3 typed refusal of join J2: an ``upstream_`` failure and no start."""
    return ModelNodeEmission(
        node=CHECK_NODE,
        piece=3,
        correlation_id=cid,
        phase="failure",
        emitted_at=FAILED_AT,
        failure_code=code,
    )


def _success_chain(cid: str) -> tuple[ModelNodeEmission, ModelNodeEmission]:
    start = ModelNodeEmission(
        node=CHECK_NODE,
        piece=3,
        correlation_id=cid,
        phase="start",
        emitted_at=FAILED_AT,
    )
    return start, start.model_copy(
        update={"phase": "success", "emitted_at": FAILED_AT + timedelta(seconds=1)}
    )


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
        clock=lambda: NOW,
    )


def _fold(sink: ProtocolEmissionSink) -> ModelFloorReceipts:
    """The floor's own receipt fold, judged for join J3, over the journal alone (no ledger piece events)."""
    return HandlerFloorReceipt().handle(
        ModelFloorReceiptInput(
            now=NOW,
            emissions=sink.read(NOW - timedelta(hours=48)),
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


def _codes(receipts: ModelFloorReceipts) -> list[str]:
    return [f.code for f in receipts.findings]


class TestGoldenChain:
    def test_the_floor_reads_the_journal_failure_unanswered(self) -> None:
        """Baseline: the board's own finding, before the node runs."""
        sink = InMemoryEmissionSink()
        _emit(sink, *_failed_chain("aa" * 16))
        receipts = _fold(sink)
        assert _codes(receipts) == ["downstream_missing_typed_failure"]
        assert receipts.joins[0].state == "broken"

    def test_a_real_journal_failure_is_answered_and_j3_reads_as_an_error_join(
        self, tmp_path: Path
    ) -> None:
        sink = InMemoryEmissionSink()
        _emit(sink, *_failed_chain("aa" * 16))
        result = _handler(sink, _empty_ledger(tmp_path)).handle(
            ModelJoinRefusalCommand(write=True)
        )
        assert result.ok
        assert result.wrote
        [reading] = result.readings
        assert (
            reading.status,
            reading.upstream_failures,
            reading.owed,
            reading.written,
        ) == (
            "owed",
            1,
            1,
            1,
        )
        [refusal] = [e for e in sink.events if e.piece == 4]
        assert refusal.node == DOWNSTREAM_NODE
        assert refusal.correlation_id == "aa" * 16
        assert refusal.phase == "failure"
        assert refusal.failure_code == "upstream_TopicAuthorizationFailedError"
        receipts = _fold(sink)
        assert _codes(receipts) == []
        assert receipts.joins[0].state == "error_only"

    def test_every_failed_run_is_answered_under_its_own_id_and_none_twice(
        self, tmp_path: Path
    ) -> None:
        sink = InMemoryEmissionSink()
        for index in range(5):
            _emit(
                sink,
                *_failed_chain(
                    f"{index:02d}" * 16, at=FAILED_AT + timedelta(minutes=index)
                ),
            )
        handler = _handler(sink, _empty_ledger(tmp_path))
        handler.handle(ModelJoinRefusalCommand(write=True))
        again = handler.handle(ModelJoinRefusalCommand(write=True))
        refusals = [e for e in sink.events if e.piece == 4]
        assert sorted(e.correlation_id for e in refusals) == [
            f"{index:02d}" * 16 for index in range(5)
        ]
        assert not again.wrote
        assert (again.readings[0].status, again.readings[0].answered) == ("answered", 5)
        assert _codes(_fold(sink)) == []

    def test_a_failure_still_inside_the_deadline_is_answered_in_the_same_run(
        self, tmp_path: Path
    ) -> None:
        sink = InMemoryEmissionSink()
        _emit(sink, *_failed_chain("bb" * 16, at=NOW - timedelta(seconds=30)))
        _handler(sink, _empty_ledger(tmp_path)).handle(
            ModelJoinRefusalCommand(write=True)
        )
        assert [e.correlation_id for e in sink.events if e.piece == 4] == ["bb" * 16]

    def test_the_real_journal_backend_gives_the_same_answer(
        self, tmp_path: Path
    ) -> None:
        sink = JsonlEmissionSink(tmp_path / "journal" / "emissions.jsonl")
        _emit(sink, *_failed_chain("aa" * 16))
        _handler(sink, _empty_ledger(tmp_path)).handle(
            ModelJoinRefusalCommand(write=True)
        )
        receipts = _fold(sink)
        assert _codes(receipts) == []
        assert receipts.joins[0].state == "error_only"

    def test_probe_writes_nothing_and_says_what_is_owed(self, tmp_path: Path) -> None:
        sink = InMemoryEmissionSink()
        _emit(sink, *_failed_chain("aa" * 16))
        before = len(sink.events)
        result = _handler(sink, _empty_ledger(tmp_path)).handle(
            ModelJoinRefusalCommand(write=False)
        )
        assert len(sink.events) == before
        assert not result.wrote
        assert (result.readings[0].owed, result.readings[0].written) == (1, 0)


class TestErrorChain:
    def test_a_drill_is_not_answered(self, tmp_path: Path) -> None:
        sink = InMemoryEmissionSink()
        _emit(sink, *_failed_chain("cc" * 16, drill=True))
        result = _handler(sink, _empty_ledger(tmp_path)).handle(
            ModelJoinRefusalCommand(write=True)
        )
        assert [e for e in sink.events if e.piece == 4] == []
        assert result.readings[0].drills_skipped == 1
        assert result.readings[0].owed == 0
        assert _codes(_fold(sink)) == []

    def test_a_piece_3_refusal_is_not_a_failure_to_answer(self, tmp_path: Path) -> None:
        sink = InMemoryEmissionSink()
        _emit(sink, _refusal_chain("dd" * 16))
        result = _handler(sink, _empty_ledger(tmp_path)).handle(
            ModelJoinRefusalCommand(write=True)
        )
        assert [e for e in sink.events if e.piece == 4] == []
        assert result.readings[0].upstream_failures == 0
        assert _codes(_fold(sink)) == []

    def test_an_upstream_coded_failure_that_has_a_start_is_a_real_failure(
        self, tmp_path: Path
    ) -> None:
        sink = InMemoryEmissionSink()
        _emit(sink, *_failed_chain("ee" * 16, code="upstream_but_started"))
        _handler(sink, _empty_ledger(tmp_path)).handle(
            ModelJoinRefusalCommand(write=True)
        )
        [refusal] = [e for e in sink.events if e.piece == 4]
        assert refusal.failure_code == "upstream_upstream_but_started"

    def test_a_success_is_not_answered(self, tmp_path: Path) -> None:
        sink = InMemoryEmissionSink()
        _emit(sink, *_success_chain("ff" * 16))
        result = _handler(sink, _empty_ledger(tmp_path)).handle(
            ModelJoinRefusalCommand(write=True)
        )
        assert [e for e in sink.events if e.piece == 4] == []
        assert result.readings[0].upstream_failures == 0

    def test_a_node_the_overlay_does_not_name_is_not_answered(
        self, tmp_path: Path
    ) -> None:
        sink = InMemoryEmissionSink()
        _emit(sink, *_failed_chain("ab" * 16, node="example_other_node:read"))
        result = _handler(sink, _empty_ledger(tmp_path)).handle(
            ModelJoinRefusalCommand(write=True)
        )
        assert [e for e in sink.events if e.piece == 4] == []
        assert result.readings[0].upstream_failures == 0

    def test_a_join_without_journal_upstream_answers_ledger_chains_only(
        self, tmp_path: Path
    ) -> None:
        sink = InMemoryEmissionSink()
        _emit(sink, *_failed_chain("ac" * 16))
        result = _handler(
            sink, _empty_ledger(tmp_path), overlay=_overlay(journal_nodes=None)
        ).handle(ModelJoinRefusalCommand(write=True))
        assert [e for e in sink.events if e.piece == 4] == []
        assert not result.wrote

    def test_an_unreadable_ledger_is_unread_and_writes_nothing(self) -> None:
        sink = InMemoryEmissionSink()
        _emit(sink, *_failed_chain("ad" * 16))
        result = _handler(sink, _Unreadable()).handle(
            ModelJoinRefusalCommand(write=True)
        )
        assert not result.ok
        assert not result.wrote
        assert [e for e in sink.events if e.piece == 4] == []
        assert result.readings[0].status == "unread"

    def test_a_piece_4_chain_already_under_the_id_is_not_overwritten(
        self, tmp_path: Path
    ) -> None:
        sink = InMemoryEmissionSink()
        _emit(sink, *_failed_chain("ae" * 16))
        _emit(
            sink,
            _refusal_chain("ae" * 16, code="upstream_already_there").model_copy(
                update={"node": DOWNSTREAM_NODE, "piece": 4}
            ),
        )
        result = _handler(sink, _empty_ledger(tmp_path)).handle(
            ModelJoinRefusalCommand(write=True)
        )
        assert len([e for e in sink.events if e.piece == 4]) == 1
        assert result.readings[0].answered == 1


class TestOverlayShape:
    def test_journal_upstream_must_name_a_node(self) -> None:
        with pytest.raises(ValidationError):
            _overlay(journal_nodes=())

    def test_journal_upstream_refuses_a_node_named_twice(self) -> None:
        with pytest.raises(ValidationError):
            _overlay(journal_nodes=(CHECK_NODE, CHECK_NODE))


class TestFailingControl:
    def test_with_the_piece_4_refusal_dropped_j3_reads_broken_again(
        self, tmp_path: Path
    ) -> None:
        sink = _DropsPiece(4)
        _emit(sink, *_failed_chain("aa" * 16))
        _handler(sink, _empty_ledger(tmp_path)).handle(
            ModelJoinRefusalCommand(write=True)
        )
        broken = _fold(sink)
        assert broken.joins[0].state == "broken"
        assert _codes(broken) == ["downstream_missing_typed_failure"]
        assert [(f.subject, f.correlation_id) for f in broken.findings] == [
            ("J3 piece 3 to 4", "aa" * 16)
        ]

    def test_with_the_journal_source_removed_from_the_overlay_j3_reads_broken_again(
        self, tmp_path: Path
    ) -> None:
        sink = InMemoryEmissionSink()
        _emit(sink, *_failed_chain("aa" * 16))
        _handler(
            sink, _empty_ledger(tmp_path), overlay=_overlay(journal_nodes=None)
        ).handle(ModelJoinRefusalCommand(write=True))
        assert _fold(sink).joins[0].state == "broken"
