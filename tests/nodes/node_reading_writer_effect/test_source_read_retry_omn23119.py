# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A piece's source read that times out is asked again in the same run, as many passes as the overlay declares (OMN-23119).

At 2026-11-08T08:31Z pieces 4, 5 and 6 read UNKNOWN on ``read-command-exit-1-timeouterror``. Run by hand on the host that
writes the readings, the ledger read command took 7 s for an empty query and, on the same query, between 7 s and past its
own 60 s bound: the bus reply is slow and intermittently slower, so the source is slow and not mis-addressed. The reading is
not made fresher by a longer bound; it is asked again. Each control fails with the retry removed:

- a source read that times out once is read again and the piece reads WORKING;
- a source that times out on every pass still reads UNKNOWN (never WORKING) and the pass count is the overlay's;
- a clean run never pauses;
- with no ``read_retry`` in the overlay the writer behaves as before.
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

PAUSE_S = 5
SOURCE_LANE = "work-ledger-parity"


class SlowSourceLedger(InMemoryLedger):
    """Times out the first ``timeouts`` reads of piece 6's source lane, as the ledger bus did at 08:31Z."""

    def __init__(self, *, timeouts: int) -> None:
        super().__init__(healthy_ledger())
        self.timeouts = timeouts
        self.source_reads = 0

    def read(self, query: ModelLedgerQuery) -> ModelLedgerRead:
        if query.lane != SOURCE_LANE:
            return super().read(query)
        self.source_reads += 1
        if self.source_reads <= self.timeouts:
            return ModelLedgerRead(error="read-command-exit-1-timeouterror")
        return super().read(query)


def _overlay(passes: int | None) -> ModelReadingOverlay:
    document = overlay_document()
    if passes is not None:
        document["read_retry"] = {"passes": passes, "pause_seconds": PAUSE_S}
    return ModelReadingOverlay.model_validate(document)


def _run(
    ledger: SlowSourceLedger, passes: int | None
) -> tuple[dict[int, ModelReadingPieceOutcome], list[float]]:
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
            correlation_id="c-src-retry", window_start=START, pieces=(), write=True
        )
    )
    assert result.status == "completed"
    return {o.piece: o for o in result.pieces}, pauses


def test_error_without_a_retry_one_timed_out_source_read_reads_the_piece_unknown() -> (
    None
):
    """The failing control: the board of 2026-11-08T08:31Z."""
    ledger = SlowSourceLedger(timeouts=1)
    outcomes, pauses = _run(ledger, None)
    assert outcomes[6].outcome is EnumReadingOutcome.SOURCE_UNREADABLE
    assert outcomes[6].verdict is EnumReadingVerdict.UNKNOWN
    assert ledger.source_reads == 1
    assert pauses == []


def test_golden_a_timed_out_source_read_is_read_again_and_the_piece_reads_working() -> (
    None
):
    ledger = SlowSourceLedger(timeouts=1)
    outcomes, pauses = _run(ledger, 1)
    assert outcomes[6].outcome is EnumReadingOutcome.READ
    assert outcomes[6].verdict is EnumReadingVerdict.WORKING
    assert ledger.source_reads == 2
    assert pauses == [PAUSE_S]


def test_error_a_source_that_times_out_on_every_pass_still_reads_unknown() -> None:
    """A reading that can fail for real: the retry never turns a dead source into WORKING."""
    ledger = SlowSourceLedger(timeouts=99)
    outcomes, pauses = _run(ledger, 2)
    assert outcomes[6].outcome is EnumReadingOutcome.SOURCE_UNREADABLE
    assert outcomes[6].verdict is EnumReadingVerdict.UNKNOWN
    assert ledger.source_reads == 3  # the first read and the two retries
    assert pauses == [PAUSE_S, PAUSE_S]


def test_golden_a_clean_run_never_pauses_or_reads_twice() -> None:
    ledger = SlowSourceLedger(timeouts=0)
    outcomes, pauses = _run(ledger, 3)
    assert outcomes[6].verdict is EnumReadingVerdict.WORKING
    assert ledger.source_reads == 1
    assert pauses == []
