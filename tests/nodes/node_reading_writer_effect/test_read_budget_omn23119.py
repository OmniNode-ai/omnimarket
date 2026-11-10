# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A per-cycle budget of seconds spent on failed source reads keeps a stalled bus from using up the unit (OMN-23519).

At 2026-11-10T10:15Z the hourly reading unit ran its whole 600 s start limit and systemd stopped it: piece 1's source read
timed out on every pass, the reads after it were slow, and pieces 10 to 18 were never written (they kept the 09:15Z
reading). One failed query costs up to 200 s (three 60 s bus timeouts and two 10 s pauses) and an error is never kept, so
every piece that asks the same failing query pays again. The overlay's ``read_retry.budget_seconds`` bounds the seconds one
cycle spends on failed reads: once spent, a read is tried once, and the first such read that fails stops the later reads
of the cycle (``read-budget-spent``). A read that answers after the budget is spent keeps the cycle going, so a transient
stall costs the one piece whose read failed and nothing else. Each control fails with the budget removed:

- a bus that stalls on every read, without a budget, runs past the unit's limit; with the budget it stays inside it, every
  declared piece still has a reading, and the stopped reads carry ``read-budget-spent``;
- a bus that stalls for one query and then answers, with the budget spent by that query, leaves every other piece read;
- with no ``budget_seconds`` in the overlay the writer behaves as before;
- the budget is an overlay value: the handler holds no number for it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.models.model_reading import (
    EnumReadingOutcome,
    EnumReadingVerdict,
    ModelReadingWindowCommand,
    ModelReadingWindowResult,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)
from omnimarket.nodes.node_reading_writer_effect.handlers.handler_reading_writer import (
    HandlerReadingWriter,
    answer_command,
    load_topics,
)
from omnimarket.nodes.node_reading_writer_effect.handlers.schedule import (
    load_schedule,
)
from omnimarket.nodes.node_reading_writer_effect.overlay import (
    ModelReadingOverlay,
    ModelReadingReadRetry,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import (
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

TOPICS = load_topics()
STALL_S = 60  # one bus timeout, as the read command's own bound is
PAUSE_S = 10
PASSES = 2
BUDGET_S = 100  # a test value; the lab value is the overlays repository's
#: The reading unit's start limit, read from the node contract's schedule block.
UNIT_LIMIT_S = load_schedule().timeout_seconds
STALLED_LANES = ("work-ledger-parity", "landing-L1")


class FakeClock:
    """The run's clock; the stalled ledger and the pause advance it, so elapsed time is the sum of both."""

    def __init__(self) -> None:
        self.now = RUN_AT
        self.pauses: list[float] = []

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)

    def pause(self, seconds: float) -> None:
        self.pauses.append(seconds)
        self.advance(seconds)

    def read(self) -> datetime:
        return self.now

    def elapsed(self) -> float:
        return (self.now - RUN_AT).total_seconds()


class StalledLedger(InMemoryLedger):
    """Answers the first ``healthy_reads`` reads, then times out every read after ``fail_from`` for ``fail_count`` reads.

    ``fail_count`` of None fails every read from ``fail_from`` on (a bus that stays stalled).
    """

    def __init__(
        self, fake: FakeClock, *, fail_from: int = 0, fail_count: int | None = None
    ) -> None:
        super().__init__(healthy_ledger())
        self.fake = fake
        self.fail_from = fail_from
        self.fail_count = fail_count
        self.source_calls = 0

    def read(self, query: ModelLedgerQuery) -> ModelLedgerRead:
        self.source_calls += 1
        index = self.source_calls - 1
        failing = index >= self.fail_from and (
            self.fail_count is None or index < self.fail_from + self.fail_count
        )
        if failing:
            self.fake.advance(STALL_S)
            return ModelLedgerRead(error="read-command-exit-1-timeouterror")
        return super().read(query)


def _overlay(budget: int | None) -> ModelReadingOverlay:
    document = overlay_document()
    retry: dict[str, object] = {"passes": PASSES, "pause_seconds": PAUSE_S}
    if budget is not None:
        retry["budget_seconds"] = budget
    document["read_retry"] = retry
    return ModelReadingOverlay.model_validate(document)


def _writer(
    ledger: InMemoryLedger, fake: FakeClock, budget: int | None
) -> HandlerReadingWriter:
    return HandlerReadingWriter(
        overlay=_overlay(budget),
        source=ledger,
        appender=ledger,
        clock=fake.read,
        pause=fake.pause,
    )


def _command() -> ModelReadingWindowCommand:
    return ModelReadingWindowCommand(
        correlation_id="c-read-budget", window_start=START, pieces=(), write=True
    )


def _run(
    ledger: InMemoryLedger, fake: FakeClock, budget: int | None
) -> ModelReadingWindowResult:
    result = _writer(ledger, fake, budget).handle(_command())
    assert result.status == "completed"
    return result


def _reasons(result: ModelReadingWindowResult) -> Iterable[str]:
    return (reason for piece in result.pieces for reason in piece.reasons)


def _working(result: ModelReadingWindowResult) -> set[int]:
    return {
        piece.piece
        for piece in result.pieces
        if piece.verdict is EnumReadingVerdict.WORKING
    }


def test_error_without_a_budget_a_stalled_bus_runs_past_the_unit_limit() -> None:
    """The failing control: the 10:15Z run. Every ask of a stalled source pays all its passes and the run passes 600 s."""
    fake = FakeClock()
    ledger = StalledLedger(fake)
    result = _run(ledger, fake, None)
    assert fake.elapsed() > UNIT_LIMIT_S
    assert len(result.pieces) == 3
    assert "read-budget-spent" not in set(_reasons(result))


def test_error_a_stalled_bus_with_a_budget_stays_inside_the_unit_limit_and_names_the_stop() -> (
    None
):
    """With the budget the same stalled run ends inside the limit, every piece has a reading row, and the stop is named."""
    fake = FakeClock()
    ledger = StalledLedger(fake)
    result = _run(ledger, fake, BUDGET_S)
    assert fake.elapsed() < UNIT_LIMIT_S
    # the budget runs out inside one query's retries, so that query ends one attempt late: budget + one stall + one pause
    # for the query that spends it, and one more stall for the first read that fails after it
    assert fake.elapsed() <= BUDGET_S + 2 * STALL_S + PAUSE_S
    assert [piece.piece for piece in result.pieces] == [1, 6, 15]
    outcomes = {piece.piece: piece.outcome for piece in result.pieces}
    assert outcomes[6] is EnumReadingOutcome.SOURCE_UNREADABLE
    assert outcomes[15] is EnumReadingOutcome.SOURCE_UNREADABLE
    assert any("read-budget-spent" in reason for reason in _reasons(result))
    # a piece whose replay check could not read is not written; the previous reading stands and the result says why
    unwritten = [p for p in result.pieces if p.ledger.startswith("append-failed")]
    assert all("replay-check" in p.ledger for p in unwritten)
    # the stalled bus was asked a handful of times, not once per piece and pass
    assert ledger.source_calls <= 2 * (PASSES + 1)


def test_golden_a_stall_that_ends_costs_only_the_query_whose_read_failed() -> None:
    """The 10:15Z case: one query fails and spends the budget, the bus then answers, no later read is stopped."""
    fake = FakeClock()
    # two failed attempts spend the budget: 60 s, a 10 s pause, 60 s is 130 s against 100 s, so no third pass is made
    ledger = StalledLedger(fake, fail_from=0, fail_count=2)
    result = _run(ledger, fake, BUDGET_S)
    healthy_fake = FakeClock()
    healthy = _run(StalledLedger(healthy_fake, fail_from=10**6), healthy_fake, BUDGET_S)
    assert fake.elapsed() == 2 * STALL_S + PAUSE_S
    assert "read-budget-spent" not in set(_reasons(result))
    assert _working(result) == _working(healthy) == {6, 15}
    unwritten = [p for p in result.pieces if p.ledger.startswith("append-failed")]
    assert [p.piece for p in unwritten] == [1]


def test_golden_a_clean_run_with_a_budget_skips_nothing_and_never_pauses() -> None:
    """A healthy bus with a budget set reads every piece as it does without one."""
    fake = FakeClock()
    result = _run(StalledLedger(fake, fail_from=10**6), fake, BUDGET_S)
    assert fake.pauses == []
    assert fake.elapsed() == 0
    assert "read-budget-spent" not in set(_reasons(result))
    assert _working(result) == {6, 15}


def test_golden_with_no_budget_in_the_overlay_the_writer_reads_as_before() -> None:
    """The exemplar's retry behaviour is unchanged: one timed-out read is read again and the piece reads WORKING."""
    fake = FakeClock()
    ledger = StalledLedger(fake, fail_from=0, fail_count=1)
    result = _run(ledger, fake, None)
    assert fake.pauses == [PAUSE_S]
    assert _working(result) == {6, 15}


def test_chain_golden_a_clean_window_with_a_budget_publishes_every_piece_and_completes() -> (
    None
):
    """Golden chain: the command in, one piece event per declared piece, then the completion, none naming the budget."""
    fake = FakeClock()
    ledger = StalledLedger(fake, fail_from=10**6)
    events = answer_command(
        _command().model_dump_json().encode(), _writer(ledger, fake, BUDGET_S), TOPICS
    )
    bodies = [(topic, json.loads(body)) for topic, body in events]
    assert [topic for topic, _ in bodies] == [
        TOPICS["reading_piece_written"],
        TOPICS["reading_piece_written"],
        TOPICS["reading_piece_written"],
        TOPICS["reading_window_completed"],
    ]
    assert {body["correlation_id"] for _, body in bodies} == {"c-read-budget"}
    assert bodies[-1][1]["failed"] == 0
    assert all("read-budget-spent" not in json.dumps(body) for _, body in bodies)


def test_chain_error_a_stalled_window_publishes_a_failure_event_per_stopped_piece_and_completes() -> (
    None
):
    """Error chain: the stopped pieces are typed piece-failed events carrying the named reason, and the window still completes."""
    fake = FakeClock()
    ledger = StalledLedger(fake)
    events = answer_command(
        _command().model_dump_json().encode(), _writer(ledger, fake, BUDGET_S), TOPICS
    )
    bodies = [(topic, json.loads(body)) for topic, body in events]
    assert bodies[-1][0] == TOPICS["reading_window_completed"]
    assert {body["correlation_id"] for _, body in bodies} == {"c-read-budget"}
    failed = [body for topic, body in bodies if topic == TOPICS["reading_piece_failed"]]
    assert {body["piece"] for body in failed} >= {6, 15}
    assert any("read-budget-spent" in json.dumps(body["reasons"]) for body in failed)
    assert fake.elapsed() < UNIT_LIMIT_S


def test_the_budget_value_is_an_overlay_value_and_not_a_number_in_the_handler() -> None:
    """The handler source holds no assignment of a number to a budget; the value is read from the overlay."""
    source = (
        Path(__file__).resolve().parents[3]
        / "src/omnimarket/nodes/node_reading_writer_effect/handlers/handler_reading_writer.py"
    ).read_text(encoding="utf-8")
    assert "retry.budget_seconds" in source
    assert not re.search(r"budget\w*\s*[:=]\s*\d", source, flags=re.IGNORECASE)


def test_the_overlay_budget_is_optional_and_positive() -> None:
    """The overlay validates without budget_seconds, and a budget_seconds of 0 is refused."""
    assert (
        ModelReadingReadRetry.model_validate(
            {"passes": 2, "pause_seconds": 10}
        ).budget_seconds
        is None
    )
    with pytest.raises(ValidationError):
        ModelReadingReadRetry.model_validate(
            {"passes": 2, "pause_seconds": 10, "budget_seconds": 0}
        )
