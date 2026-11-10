# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A piece whose append failed is written again in the same run, so one failed tick cannot age it past its bound (OMN-23119).

The writer's 06:15Z tick on 2026-11-08 appended 16 of 18 rows; pieces 6 and 7 stayed at their 05:15Z reading and read
7,257 s old against a 7,200 s bound at 07:16Z. Three of 19 hourly runs since 2026-11-07T12Z appended 16 rows, and the
tick wrote nothing again until the next hour. Each control below fails with the retry removed and passes with it:

- a piece whose first append fails is written by the retry pass;
- a piece that fails every pass keeps its append-failed outcome and the pass count is the overlay's;
- a row that landed before its reply was lost reads back as already written, never a second row;
- with no ``append_retry`` in the overlay the writer behaves as before.
"""

from __future__ import annotations

import pytest

from omnimarket.models.model_reading import (
    EnumReadingOutcome,
    EnumReadingVerdict,
    ModelReadingPieceOutcome,
    ModelReadingWindowCommand,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)
from omnimarket.nodes.node_reading_writer_effect.handlers.handler_reading_writer import (
    HandlerReadingWriter,
)
from omnimarket.nodes.node_reading_writer_effect.overlay import ModelReadingOverlay
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ModelAppendOutcome,
    ModelLedgerQuery,
    ModelLedgerRead,
)
from tests.nodes.node_reading_writer_effect.reading_support import (
    RUN_AT,
    START,
    healthy_ledger,
    overlay_document,
)

pytestmark = pytest.mark.unit

PAUSE_S = 7


class FlakyLedger(InMemoryLedger):
    """Fails the appends of ``pieces`` for their first ``failures`` attempts; ``lose_reply`` stores the row and still fails."""

    def __init__(
        self,
        *,
        pieces: tuple[int, ...],
        failures: int,
        lose_reply: bool = False,
    ) -> None:
        super().__init__(healthy_ledger())
        self.pieces = pieces
        self.failures = failures
        self.lose_reply = lose_reply
        self.attempts: dict[int, int] = {}

    def append(self, rows: str, request_id: str) -> ModelAppendOutcome:
        for piece in self.pieces:
            if f"| piece={piece} |" in rows:
                self.attempts[piece] = self.attempts.get(piece, 0) + 1
                if self.attempts[piece] <= self.failures:
                    if self.lose_reply:
                        super().append(rows, request_id)
                    return ModelAppendOutcome(status="error", message="reply timed out")
        return super().append(rows, request_id)


def _overlay(passes: int | None) -> ModelReadingOverlay:
    document = overlay_document()
    if passes is not None:
        document["append_retry"] = {"passes": passes, "pause_seconds": PAUSE_S}
    return ModelReadingOverlay.model_validate(document)


def _run(ledger: FlakyLedger, passes: int | None) -> tuple[dict[int, str], list[float]]:
    pauses: list[float] = []
    writer = HandlerReadingWriter(
        overlay=_overlay(passes),
        source=ledger,
        appender=ledger,
        clock=lambda: RUN_AT,
        pause=pauses.append,
    )
    result = writer.handle(
        ModelReadingWindowCommand(
            correlation_id="c-retry", window_start=START, pieces=(), write=True
        )
    )
    assert result.status == "completed"
    return {o.piece: o.ledger for o in result.pieces}, pauses


def _rows(ledger: FlakyLedger, piece: int) -> list[str]:
    return [
        line
        for line in ledger.lines
        if "src=reading" in line and f"| piece={piece} |" in line
    ]


def test_error_without_a_retry_a_failed_append_leaves_the_piece_unwritten() -> None:
    """The failing control: the board of 2026-11-08, pieces 6 and 15 fail once and nothing writes them again."""
    ledger = FlakyLedger(pieces=(6, 15), failures=1)
    outcomes, pauses = _run(ledger, None)
    assert outcomes[6].startswith("append-failed")
    assert outcomes[15].startswith("append-failed")
    assert _rows(ledger, 6) == []
    assert pauses == []


def test_golden_a_failed_append_is_written_by_the_retry_pass() -> None:
    ledger = FlakyLedger(pieces=(6, 15), failures=1)
    outcomes, pauses = _run(ledger, 1)
    assert outcomes[6] == "written"
    assert outcomes[15] == "written"
    assert len(_rows(ledger, 6)) == 1
    assert len(_rows(ledger, 15)) == 1
    assert pauses == [PAUSE_S]
    assert not [p for p, o in outcomes.items() if o.startswith("append-failed")]


def test_golden_a_clean_run_never_pauses_or_retries() -> None:
    ledger = FlakyLedger(pieces=(), failures=0)
    outcomes, pauses = _run(ledger, 3)
    assert pauses == []
    assert set(outcomes.values()) == {"written"}


def test_error_a_piece_that_fails_every_pass_keeps_its_append_failed_outcome() -> None:
    ledger = FlakyLedger(pieces=(6,), failures=99)
    outcomes, pauses = _run(ledger, 2)
    assert outcomes[6].startswith("append-failed")
    assert ledger.attempts[6] == 3  # the first pass and the two retries
    assert pauses == [PAUSE_S, PAUSE_S]
    assert _rows(ledger, 6) == []
    # The other pieces are written once, whatever piece 6 does.
    assert all(o == "written" for p, o in outcomes.items() if p != 6)


def test_error_a_row_that_landed_before_its_reply_was_lost_is_not_written_twice() -> (
    None
):
    ledger = FlakyLedger(pieces=(6,), failures=1, lose_reply=True)
    outcomes, _ = _run(ledger, 1)
    assert outcomes[6] == "already-written"
    assert len(_rows(ledger, 6)) == 1


class SlowBusLedger(InMemoryLedger):
    """Counts the replay-check reads (the reading-writer lane) and times out the first ``timeouts`` of them."""

    def __init__(self, *, timeouts: int) -> None:
        super().__init__(healthy_ledger())
        self.timeouts = timeouts
        self.replay_reads = 0

    def read(self, query: ModelLedgerQuery) -> ModelLedgerRead:
        if query.lane != "reading-writer":
            return super().read(query)
        self.replay_reads += 1
        if self.replay_reads <= self.timeouts:
            return ModelLedgerRead(error="read-command-exit-1-timeouterror")
        return super().read(query)


def _slow_run(ledger: SlowBusLedger, passes: int | None) -> dict[int, str]:
    writer = HandlerReadingWriter(
        overlay=_overlay(passes),
        source=ledger,
        appender=ledger,
        clock=lambda: RUN_AT,
        pause=lambda _seconds: None,
    )
    result = writer.handle(
        ModelReadingWindowCommand(
            correlation_id="c-slow", window_start=START, pieces=(), write=True
        )
    )
    return {o.piece: o.ledger for o in result.pieces}


def test_golden_one_answer_of_the_ledger_serves_every_piece_of_a_pass() -> None:
    ledger = SlowBusLedger(timeouts=0)
    outcomes = _slow_run(ledger, None)
    assert set(outcomes.values()) == {"written"}
    assert ledger.replay_reads == 1


def test_error_a_timed_out_read_is_not_kept_and_the_next_piece_asks_again() -> None:
    """The board of 2026-11-08: one reply times out and costs one piece, not the pass."""
    ledger = SlowBusLedger(timeouts=1)
    outcomes = _slow_run(ledger, None)
    failed = [
        p for p, o in outcomes.items() if o.startswith("append-failed:replay-check")
    ]
    assert len(failed) == 1
    assert ledger.replay_reads == 2


def test_golden_the_retry_pass_asks_the_ledger_again() -> None:
    ledger = SlowBusLedger(timeouts=1)
    outcomes = _slow_run(ledger, 1)
    assert set(outcomes.values()) == {"written"}
    assert ledger.replay_reads == 3


class FlakySourceLedger(InMemoryLedger):
    """Answers every query until ``break_sources``; then every query that is not the writer's own times out."""

    def __init__(self) -> None:
        super().__init__(healthy_ledger())
        self.break_sources = False

    def read(self, query: ModelLedgerQuery) -> ModelLedgerRead:
        if self.break_sources and query.lane != "reading-writer":
            return ModelLedgerRead(error="read-command-exit-1-timeouterror")
        return super().read(query)


def _window(ledger: FlakySourceLedger) -> dict[int, ModelReadingPieceOutcome]:
    writer = HandlerReadingWriter(
        overlay=_overlay(None),
        source=ledger,
        appender=ledger,
        clock=lambda: RUN_AT,
        pause=lambda _seconds: None,
    )
    result = writer.handle(
        ModelReadingWindowCommand(
            correlation_id="c-src", window_start=START, pieces=(), write=True
        )
    )
    return {o.piece: o for o in result.pieces}


def test_error_a_source_that_times_out_later_does_not_replace_the_computed_reading() -> (
    None
):
    """Piece 6 read WORKING at 08:15Z; a run at 08:31Z whose parity read timed out wrote UNKNOWN over it."""
    ledger = FlakySourceLedger()
    first = _window(ledger)
    assert first[6].verdict is EnumReadingVerdict.WORKING
    assert first[6].ledger == "written"
    ledger.break_sources = True
    second = _window(ledger)
    assert second[6].outcome is EnumReadingOutcome.SOURCE_UNREADABLE
    assert second[6].ledger == "kept-computed-reading"
    assert len(_rows(ledger, 6)) == 1


def test_golden_a_source_unreadable_with_no_computed_reading_is_still_recorded() -> (
    None
):
    ledger = FlakySourceLedger()
    ledger.break_sources = True
    outcomes = _window(ledger)
    assert outcomes[6].outcome is EnumReadingOutcome.SOURCE_UNREADABLE
    assert outcomes[6].ledger == "written"
    assert len(_rows(ledger, 6)) == 1
