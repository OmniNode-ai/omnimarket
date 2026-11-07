# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The downstream piece's typed refusal (OMN-23022): golden and error chain event tests, and the failing control.

The floor reads a piece 2 host-canary reading that is NOT WORKING as a failure chain of piece 2, under a correlation id the
ledger row decides. Join J2 (piece 2 to piece 3) then wants a typed ``upstream_`` failure of piece 3 under that id and no
start. Before this node nothing answered, so each failure read ``downstream_missing_typed_failure`` after the chain deadline
and the board's ``chains:broken`` row stayed red.

Golden: a real failure with no answer is answered once, and the floor's own receipt fold then judges the join as an error
join with no finding. Error: a drill, a success and an unreadable ledger are not answered. Failing control: the refusal
dropped from the journal turns the join broken again, with the same finding the board shows.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
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
)
from omnimarket.nodes.node_floor_emission_compute.handlers.handler_floor_receipt import (
    HandlerFloorReceipt,
)
from omnimarket.nodes.node_floor_simple_effect.handlers.ledger_reads import (
    piece_events,
)
from omnimarket.nodes.node_join_refusal_effect import HandlerJoinRefusal
from omnimarket.nodes.node_join_refusal_effect.models import (
    ModelJoinRefusalCommand,
)
from omnimarket.nodes.node_join_refusal_effect.overlay import (
    ModelJoinRefusalOverlay,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    FileLedgerRowSource,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ModelLedgerQuery,
    ModelLedgerRead,
    ProtocolLedgerRowSource,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 7, 18, 0, tzinfo=UTC)
DEADLINE_S = 900
OVERLAY_EXAMPLE = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_join_refusal_effect/overlay.example.yaml"
)


def _overlay() -> ModelJoinRefusalOverlay:
    return ModelJoinRefusalOverlay.model_validate(
        yaml.safe_load(OVERLAY_EXAMPLE.read_text(encoding="utf-8"))
    )


def _row(at: datetime, verdict: str, *, drill: str = "no", reason: str = "ok") -> str:
    stamp = at.strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        f"{stamp} | STATUS | lane=example-host-reading | src=example-host-reading | probe=host-canary "
        f"| verdict={verdict} | outcome=read | reason={reason} | drill={drill} | probed_at={stamp}"
    )


def _ledger(tmp_path: Path, *rows: str) -> FileLedgerRowSource:
    live = tmp_path / "LEDGER.md"
    live.write_text("\n".join(rows) + "\n", encoding="utf-8")
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


def _handler(
    source: ProtocolLedgerRowSource, sink: ProtocolEmissionSink, *, now: datetime = NOW
) -> HandlerJoinRefusal:
    return HandlerJoinRefusal(
        overlay=_overlay(),
        source=source,
        sink=sink,
        backend="in_memory",
        clock=lambda: now,
    )


def _receipts(
    source: FileLedgerRowSource, sink: ProtocolEmissionSink, now: datetime
) -> ModelFloorReceipts:
    """The floor's own fold: the ledger rows as piece events, plus the journal, judged for join J2."""
    rule = _overlay().joins[0].upstream
    rows = source.read(
        ModelLedgerQuery(
            since=now - timedelta(days=7),
            until=now + timedelta(days=1),
            row_type="STATUS",
            lane="example-host-reading",
        )
    ).rows
    events = (*sink.read(now - timedelta(hours=48)), *piece_events(list(rows), (rule,)))
    return HandlerFloorReceipt().handle(
        ModelFloorReceiptInput(
            now=now,
            emissions=events,
            pieces=tuple(
                ModelPieceDef(piece=n, name=f"piece {n}", reading_max_age_s=86400)
                for n in range(1, 19)
            ),
            joins=(
                ModelJoinDef(id="J2", from_piece=2, to_piece=3, label="hosts to bus"),
            ),
            freshness_max_age_s=7200,
            error_max_age_s=86400,
            chain_deadline_s=DEADLINE_S,
            judged_to_pieces=(2, 3),
        )
    )


def _codes(receipts: ModelFloorReceipts) -> list[str]:
    return [f.code for f in receipts.findings]


FAILED_AT = NOW - timedelta(hours=1)


class TestGoldenChain:
    def test_the_dev_floor_reads_the_failure_unanswered(self, tmp_path: Path) -> None:
        """Baseline: the board's own finding, before the node runs."""
        source = _ledger(
            tmp_path, _row(FAILED_AT, "NOT_WORKING", reason="canary-timed-out-h101")
        )
        receipts = _receipts(source, InMemoryEmissionSink(), NOW)
        assert _codes(receipts) == ["downstream_missing_typed_failure"]
        assert receipts.joins[0].state == "broken"

    def test_a_real_failure_is_answered_and_the_join_reads_as_an_error_join(
        self, tmp_path: Path
    ) -> None:
        source = _ledger(
            tmp_path, _row(FAILED_AT, "NOT_WORKING", reason="canary-timed-out-h101")
        )
        sink = InMemoryEmissionSink()
        result = _handler(source, sink).handle(ModelJoinRefusalCommand(write=True))
        assert result.ok
        assert result.wrote
        [reading] = result.readings
        assert (reading.status, reading.owed, reading.written) == ("owed", 1, 1)
        [refusal] = sink.events
        assert refusal.piece == 3
        assert refusal.phase == "failure"
        assert refusal.failure_code == "upstream_canary-timed-out-h101"
        receipts = _receipts(source, sink, NOW)
        assert _codes(receipts) == []
        assert receipts.joins[0].state == "error_only"

    def test_the_refusal_carries_the_correlation_id_the_floor_builds_from_the_row(
        self, tmp_path: Path
    ) -> None:
        source = _ledger(tmp_path, _row(FAILED_AT, "NOT_WORKING"))
        sink = InMemoryEmissionSink()
        _handler(source, sink).handle(ModelJoinRefusalCommand(write=True))
        rule = _overlay().joins[0].upstream
        rows = source.read(
            ModelLedgerQuery(
                since=NOW - timedelta(days=7),
                until=NOW + timedelta(days=1),
                row_type="STATUS",
                lane="example-host-reading",
            )
        ).rows
        upstream = {e.correlation_id for e in piece_events(list(rows), (rule,))}
        assert {e.correlation_id for e in sink.events} == upstream

    def test_a_second_run_answers_nothing_twice(self, tmp_path: Path) -> None:
        source = _ledger(tmp_path, _row(FAILED_AT, "NOT_WORKING"))
        sink = InMemoryEmissionSink()
        handler = _handler(source, sink)
        handler.handle(ModelJoinRefusalCommand(write=True))
        again = handler.handle(ModelJoinRefusalCommand(write=True))
        assert len(sink.events) == 1
        assert not again.wrote
        assert (again.readings[0].status, again.readings[0].answered) == ("answered", 1)

    def test_the_real_journal_backend_gives_the_same_answer(
        self, tmp_path: Path
    ) -> None:
        source = _ledger(tmp_path, _row(FAILED_AT, "NOT_WORKING"))
        sink = JsonlEmissionSink(tmp_path / "journal" / "emissions.jsonl")
        _handler(source, sink).handle(ModelJoinRefusalCommand(write=True))
        assert _codes(_receipts(source, sink, NOW)) == []

    def test_probe_writes_nothing_and_says_what_is_owed(self, tmp_path: Path) -> None:
        source = _ledger(tmp_path, _row(FAILED_AT, "NOT_WORKING"))
        sink = InMemoryEmissionSink()
        result = _handler(source, sink).handle(ModelJoinRefusalCommand(write=False))
        assert sink.events == []
        assert not result.wrote
        assert result.readings[0].owed == 1
        assert result.readings[0].written == 0


class TestErrorChain:
    def test_a_drill_is_not_answered(self, tmp_path: Path) -> None:
        source = _ledger(tmp_path, _row(FAILED_AT, "NOT_WORKING", drill="yes"))
        sink = InMemoryEmissionSink()
        result = _handler(source, sink).handle(ModelJoinRefusalCommand(write=True))
        assert sink.events == []
        assert result.readings[0].drills_skipped == 1
        assert result.readings[0].owed == 0
        assert _codes(_receipts(source, sink, NOW)) == []

    def test_a_success_is_not_answered(self, tmp_path: Path) -> None:
        source = _ledger(tmp_path, _row(FAILED_AT, "WORKING"))
        sink = InMemoryEmissionSink()
        result = _handler(source, sink).handle(ModelJoinRefusalCommand(write=True))
        assert sink.events == []
        assert result.readings[0].upstream_failures == 0

    def test_an_unreadable_ledger_is_unread_and_writes_nothing(self) -> None:
        sink = InMemoryEmissionSink()
        result = _handler(_Unreadable(), sink).handle(
            ModelJoinRefusalCommand(write=True)
        )
        assert not result.ok
        assert not result.wrote
        assert sink.events == []
        assert result.readings[0].status == "unread"
        assert "ledger-file-unreadable" in result.readings[0].reason

    def test_a_failure_still_inside_the_deadline_is_answered_too(
        self, tmp_path: Path
    ) -> None:
        recent = NOW - timedelta(seconds=DEADLINE_S // 2)
        source = _ledger(tmp_path, _row(recent, "NOT_WORKING"))
        sink = InMemoryEmissionSink()
        _handler(source, sink).handle(ModelJoinRefusalCommand(write=True))
        assert len(sink.events) == 1

    def test_only_joins_limits_the_run(self, tmp_path: Path) -> None:
        source = _ledger(tmp_path, _row(FAILED_AT, "NOT_WORKING"))
        sink = InMemoryEmissionSink()
        result = _handler(source, sink).handle(
            ModelJoinRefusalCommand(write=True, only_joins=("J9",))
        )
        assert result.readings == ()
        assert sink.events == []

    def test_an_answer_ages_out_with_the_journal_window_and_is_given_again(
        self, tmp_path: Path
    ) -> None:
        source = _ledger(tmp_path, _row(FAILED_AT, "NOT_WORKING"))
        sink = InMemoryEmissionSink()
        _handler(source, sink).handle(ModelJoinRefusalCommand(write=True))
        later = NOW + timedelta(hours=49)
        again = _handler(source, sink, now=later).handle(
            ModelJoinRefusalCommand(write=True)
        )
        assert again.wrote
        assert len(sink.events) == 2


class TestFailingControl:
    def test_dropping_the_terminal_emission_turns_the_join_broken_again(
        self, tmp_path: Path
    ) -> None:
        source = _ledger(
            tmp_path,
            _row(FAILED_AT, "NOT_WORKING", reason="canary-timed-out-h101"),
            _row(
                FAILED_AT - timedelta(hours=1),
                "NOT_WORKING",
                reason="canary-timed-out-h106",
            ),
        )
        kept, dropped = InMemoryEmissionSink(), _DropsPiece(3)
        _handler(source, kept).handle(ModelJoinRefusalCommand(write=True))
        _handler(source, dropped).handle(ModelJoinRefusalCommand(write=True))
        assert _codes(_receipts(source, kept, NOW)) == []
        broken = _receipts(source, dropped, NOW)
        assert _codes(broken) == ["downstream_missing_typed_failure"] * 2
        assert broken.joins[0].state == "broken"

    def test_a_refusal_for_the_wrong_correlation_id_does_not_clear_the_join(
        self, tmp_path: Path
    ) -> None:
        source = _ledger(tmp_path, _row(FAILED_AT, "NOT_WORKING"))
        sink = InMemoryEmissionSink()
        sink.emit(
            ModelNodeEmission.model_validate(
                {
                    "node": "example_seam_node:join_j2",
                    "piece": 3,
                    "correlation_id": "not-the-row",
                    "phase": "failure",
                    "emitted_at": NOW,
                    "failure_code": "upstream_x",
                }
            )
        )
        assert _codes(_receipts(source, sink, NOW)) == [
            "downstream_missing_typed_failure"
        ]
