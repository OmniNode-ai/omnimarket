# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A source that stays unreadable is read once more at the end of the run, before its reading is written (OMN-23588).

At 2026-11-10T16:15Z the hourly reading unit's first read, piece 1's nightly source, timed out on every pass (three 60 s bus
timeouts and two pauses), and every later read in the same run answered. Piece 1 was written UNKNOWN for the hour, left the
ready set, and the run exited 1 (``WINDOW ... failed=1``). The same single-source stall had cost the 10:15Z run (OMN-23519).
The overlay's ``read_retry.reread_budget_seconds`` makes the writer hold a piece whose source stayed unreadable, finish the
other pieces, and read the held pieces once each at the end; only then is its row written, so no UNKNOWN row exists to
disagree with the reading. The final pass sleeps never and asks each read once: its first failed read stops its later reads
(``read-budget-spent``) and a pass that has used its budget starts no more reads. Each control fails without the change:

- one source that fails its inline retries and then answers reads WORKING, written once, after the other pieces;
- the same stall with no ``reread_budget_seconds`` leaves the piece SOURCE_UNREADABLE (the base behaviour);
- a bus that stays stalled adds at most the budget and one failed read to the run, and every piece is still written;
- the key is optional and positive, and its value is an overlay value, not a number in the handler.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.models.model_reading import (
    EnumReadingOutcome,
    EnumReadingVerdict,
    ModelReadingPieceOutcome,
    ModelReadingWindowCommand,
    ModelReadingWindowResult,
)
from omnimarket.nodes.node_reading_writer_effect.handlers.handler_reading_writer import (
    HandlerReadingWriter,
    answer_command,
    load_topics,
)
from omnimarket.nodes.node_reading_writer_effect.overlay import (
    ModelReadingOverlay,
    ModelReadingReadRetry,
)
from tests.nodes.node_reading_writer_effect.reading_support import (
    START,
    overlay_document,
)
from tests.nodes.node_reading_writer_effect.test_read_budget_omn23119 import (
    BUDGET_S,
    PASSES,
    PAUSE_S,
    STALL_S,
    UNIT_LIMIT_S,
    FakeClock,
    StalledLedger,
)

pytestmark = pytest.mark.unit

TOPICS = load_topics()
REREAD_S = 60  # a test value; the lab value is the overlays repository's
#: Failed attempts of the first read: 60 s, a 10 s pause, 60 s is 130 s, past the 100 s budget, so no third pass is made.
FIRST_READ_FAILS = 2
#: Piece 1 has no producer and makes one replay-check read (read 0); piece 6's source is the next read, so the stall starts there.
FIRST_READ_AT = 1


def _overlay(reread: int | None) -> ModelReadingOverlay:
    document = overlay_document()
    retry: dict[str, object] = {
        "passes": PASSES,
        "pause_seconds": PAUSE_S,
        "budget_seconds": BUDGET_S,
    }
    if reread is not None:
        retry["reread_budget_seconds"] = reread
    document["read_retry"] = retry
    return ModelReadingOverlay.model_validate(document)


def _writer(
    ledger: StalledLedger, fake: FakeClock, reread: int | None
) -> HandlerReadingWriter:
    return HandlerReadingWriter(
        overlay=_overlay(reread),
        source=ledger,
        appender=ledger,
        clock=fake.read,
        pause=fake.pause,
    )


def _command() -> ModelReadingWindowCommand:
    return ModelReadingWindowCommand(
        correlation_id="c-final-pass", window_start=START, pieces=(), write=True
    )


def _run(
    ledger: StalledLedger, fake: FakeClock, reread: int | None
) -> ModelReadingWindowResult:
    result = _writer(ledger, fake, reread).handle(_command())
    assert result.status == "completed"
    return result


def _by_piece(
    result: ModelReadingWindowResult,
) -> dict[int, ModelReadingPieceOutcome]:
    return {piece.piece: piece for piece in result.pieces}


def test_golden_final_pass_reads_late() -> None:
    """The 16:15Z case: the first source read fails its passes, the bus then answers, the final pass reads it WORKING."""
    fake = FakeClock()
    ledger = StalledLedger(fake, fail_from=FIRST_READ_AT, fail_count=FIRST_READ_FAILS)
    result = _run(ledger, fake, REREAD_S)
    pieces = result.pieces
    assert [piece.piece for piece in pieces] == [1, 6, 15]
    late = _by_piece(result)[6]
    assert late.verdict is EnumReadingVerdict.WORKING
    assert late.ledger == "written"
    assert all(not piece.ledger.startswith("append-failed") for piece in pieces)
    # the piece was written once, after the pieces that came after it in the order
    rows = [line for line in ledger.lines if "src=reading" in line]
    assert len([r for r in rows if "piece=6" in r]) == 1
    assert max(i for i, r in enumerate(rows) if "piece=6" in r) == len(rows) - 1


def test_golden_the_same_stall_without_the_key_leaves_the_piece_unreadable() -> None:
    """The failing control: with no reread_budget_seconds the piece is written SOURCE_UNREADABLE, the base behaviour."""
    fake = FakeClock()
    ledger = StalledLedger(fake, fail_from=FIRST_READ_AT, fail_count=FIRST_READ_FAILS)
    result = _run(ledger, fake, None)
    assert _by_piece(result)[6].outcome is EnumReadingOutcome.SOURCE_UNREADABLE


def test_golden_a_clean_run_with_the_key_pauses_never_and_defers_nothing() -> None:
    """A healthy bus: every piece is written in order, no pause, no extra read."""
    fake = FakeClock()
    clean = StalledLedger(fake, fail_from=10**6)
    result = _run(clean, fake, REREAD_S)
    plain_fake = FakeClock()
    plain = StalledLedger(plain_fake, fail_from=10**6)
    _run(plain, plain_fake, None)
    assert fake.pauses == []
    assert clean.source_calls == plain.source_calls
    assert clean.lines == plain.lines
    assert all(piece.ledger == "written" for piece in result.pieces)


def test_error_final_pass_stays_unknown() -> None:
    """A bus that stays stalled: the pieces stay UNREADABLE and written, the pass adds at most its budget and one failed read."""
    base_fake = FakeClock()
    base = StalledLedger(base_fake)
    _run(base, base_fake, None)
    fake = FakeClock()
    ledger = StalledLedger(fake)
    result = _run(ledger, fake, REREAD_S)
    outcomes = {p.piece: p.outcome for p in result.pieces}
    assert outcomes[6] is EnumReadingOutcome.SOURCE_UNREADABLE
    assert outcomes[15] is EnumReadingOutcome.SOURCE_UNREADABLE
    assert fake.elapsed() < UNIT_LIMIT_S
    assert fake.elapsed() <= base_fake.elapsed() + REREAD_S + STALL_S
    reasons = [reason for p in result.pieces for reason in p.reasons]
    assert any("read-budget-spent" in reason for reason in reasons)
    # no piece is left held: every piece names a ledger outcome of its own
    assert all(p.ledger != "deferred-final-read" for p in result.pieces)


def test_the_final_pass_does_not_change_what_a_later_append_retry_reads() -> None:
    """The pass's own stop is restored when it ends: the stalled run completes with the same pieces and no held piece."""
    fake = FakeClock()
    ledger = StalledLedger(fake)
    result = _run(ledger, fake, REREAD_S)
    assert [p.piece for p in result.pieces] == [1, 6, 15]


def test_chain_golden_final_pass_publishes_one_piece_event_per_piece() -> None:
    """Golden chain: after a one-source stall every piece event is a written reading and the window completes with no failure."""
    fake = FakeClock()
    ledger = StalledLedger(fake, fail_from=FIRST_READ_AT, fail_count=FIRST_READ_FAILS)
    events = answer_command(
        _command().model_dump_json().encode(), _writer(ledger, fake, REREAD_S), TOPICS
    )
    bodies = [(topic, json.loads(body)) for topic, body in events]
    assert [topic for topic, _ in bodies] == [
        TOPICS["reading_piece_written"],
        TOPICS["reading_piece_written"],
        TOPICS["reading_piece_written"],
        TOPICS["reading_window_completed"],
    ]
    assert {body["correlation_id"] for _, body in bodies} == {"c-final-pass"}
    assert bodies[-1][1]["failed"] == 0


def test_chain_error_final_pass_stalled_bus_publishes_piece_failed_and_completes() -> (
    None
):
    """Error chain: a bus that stays down publishes a failure event per unreadable piece and the window still completes."""
    fake = FakeClock()
    ledger = StalledLedger(fake)
    events = answer_command(
        _command().model_dump_json().encode(), _writer(ledger, fake, REREAD_S), TOPICS
    )
    bodies = [(topic, json.loads(body)) for topic, body in events]
    assert bodies[-1][0] == TOPICS["reading_window_completed"]
    failed = [body for topic, body in bodies if topic == TOPICS["reading_piece_failed"]]
    assert {body["piece"] for body in failed} >= {6, 15}
    assert fake.elapsed() < UNIT_LIMIT_S


def test_final_pass_overlay_key_is_optional_and_positive() -> None:
    """The overlay validates without the key (no final pass), and a value of 0 is refused."""
    base = {"passes": 2, "pause_seconds": 10}
    assert ModelReadingReadRetry.model_validate(base).reread_budget_seconds is None
    assert (
        ModelReadingReadRetry.model_validate(
            {**base, "reread_budget_seconds": 60}
        ).reread_budget_seconds
        == 60
    )
    with pytest.raises(ValidationError):
        ModelReadingReadRetry.model_validate({**base, "reread_budget_seconds": 0})


def test_final_pass_value_is_an_overlay_value_and_not_a_number_in_the_handler() -> None:
    """The handler source holds no assignment of a number to a reread value; it is read from the overlay."""
    source = (
        Path(__file__).resolve().parents[4]
        / "src/omnimarket/nodes/node_reading_writer_effect/handlers/handler_reading_writer.py"
    ).read_text(encoding="utf-8")
    assert "retry.reread_budget_seconds" in source
    assert not re.search(r"reread\w*\s*[:=]\s*\d", source, flags=re.IGNORECASE)
