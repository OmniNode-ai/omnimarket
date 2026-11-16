# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""J5 projection read (OMN-23717): a read that fails is a different outcome from a row not yet projected.

Before this change ``SourceProjectedRows.read_row`` did ``continue`` on a read error and never recorded it, so a failing read
command and a lagging projection both ended as ``no projected row``. The last read error is now carried, typed, into the
failure the chain ends with. Golden chain, error chain and failing controls run on the in-memory bus, the floor's own fold
judging the journal.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta

import pytest

from omnimarket.models.work_ledger_append import (
    EnumJoinJ5Outcome,
    ModelJoinJ5RunCommand,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ModelLedgerQuery,
    ModelLedgerRead,
    ProtocolLedgerRowSource,
)
from omnimarket.nodes.node_work_ledger_append_effect.adapters import (
    SourceProjectedRows,
)
from omnimarket.nodes.node_work_ledger_append_effect.handlers.handler_work_ledger_join_j5_run import (
    HandlerWorkLedgerJoinJ5Run,
)
from omnimarket.nodes.node_work_ledger_append_effect.protocols.join_j5 import (
    ProjectionReadFailedError,
)
from tests.unit.nodes.node_work_ledger_append_effect.test_join_j5_runtime import (
    GOLDEN_AT,
    LEDGER_ID,
    NOW,
    POLICY,
    _floor_read,
    _reading_row,
    _refuse,
    _Rig,
)

pytestmark = pytest.mark.unit

READ_ERROR = "read-command-exit-1:ProjectionUnavailable"


class _ScriptedSource:
    """A row source that answers each read from a script: an error string, or the lines it holds."""

    def __init__(self, script: Sequence[str | Sequence[str]]) -> None:
        self._script = list(script)
        self.reads = 0

    def read(self, query: ModelLedgerQuery) -> ModelLedgerRead:
        step = self._script[min(self.reads, len(self._script) - 1)]
        self.reads += 1
        if isinstance(step, str):
            return ModelLedgerRead(error=step)
        return InMemoryLedger(list(step)).read(query)


def _projected(source: ProtocolLedgerRowSource, attempts: int) -> SourceProjectedRows:
    return SourceProjectedRows(
        source,
        attempts=attempts,
        interval_seconds=0.1,
        slack_seconds=60,
        sleep=lambda _s: None,
        clock=lambda: NOW,
    )


def _runner_over(
    rig: _Rig, source: ProtocolLedgerRowSource, attempts: int = 3
) -> HandlerWorkLedgerJoinJ5Run:
    runner = rig.runner()
    runner._join._projection = _projected(source, attempts)
    return runner


ROW = _reading_row(GOLDEN_AT, "WORKING")


def test_projection_read_error_on_the_last_attempt_raises_with_the_last_error() -> None:
    source = _ScriptedSource(["read-command-exit-1", READ_ERROR])

    with pytest.raises(ProjectionReadFailedError) as raised:
        _projected(source, 2).read_row(ledger_id=LEDGER_ID, ledger_seq=7, raw_row=ROW)

    assert (raised.value.error, raised.value.attempts) == (READ_ERROR, 2)
    assert READ_ERROR in str(raised.value)
    assert source.reads == 2


def test_error_then_a_clean_answer_without_the_row_is_not_a_read_failure() -> None:
    source = _ScriptedSource([READ_ERROR, []])

    assert (
        _projected(source, 2).read_row(ledger_id=LEDGER_ID, ledger_seq=7, raw_row=ROW)
        is None
    )


def test_error_then_the_row_returns_the_row() -> None:
    source = _ScriptedSource([READ_ERROR, [ROW]])

    found = _projected(source, 2).read_row(
        ledger_id=LEDGER_ID, ledger_seq=7, raw_row=ROW
    )

    assert found is not None
    assert (found.raw_row, found.ledger_seq) == (ROW, 7)


def test_golden_a_projection_that_answers_lands_the_chain() -> None:
    rig = _Rig([ROW])

    result = rig.runner().handle(ModelJoinJ5RunCommand())

    assert [i.outcome for i in result.items] == [EnumJoinJ5Outcome.LANDED]
    assert result.items[0].detail == ""
    phases = [e.phase for e in rig.sink.events if e.piece == POLICY.to_piece]
    assert phases == ["start", "success"]


def test_error_a_projection_that_cannot_be_read_ends_the_chain_with_the_read_error() -> (
    None
):
    rig = _Rig([ROW])

    result = _runner_over(rig, _ScriptedSource([READ_ERROR])).handle(
        ModelJoinJ5RunCommand()
    )
    _refuse(rig, NOW + timedelta(minutes=2))

    item = result.items[0]
    assert item.outcome is EnumJoinJ5Outcome.PROJECTION_READ_FAILED
    assert item.failure_code == POLICY.projection_read_failed_failure_code
    assert READ_ERROR in item.detail
    mine = [e for e in rig.sink.events if e.piece == POLICY.to_piece]
    assert [e.phase for e in mine] == ["start", "failure"]
    assert mine[-1].failure_code == POLICY.projection_read_failed_failure_code
    assert READ_ERROR in mine[-1].failure_detail
    # A piece 5 success answered by a piece 6 typed failure reads broken on the floor, never silent.
    assert _floor_read(rig, NOW + timedelta(minutes=5)) == (
        "broken",
        ["downstream_failed_after_upstream_succeeded"],
    )


def test_control_a_row_not_yet_projected_keeps_its_own_outcome_and_code() -> None:
    rig = _Rig([ROW])

    result = _runner_over(rig, _ScriptedSource([[], [], []])).handle(
        ModelJoinJ5RunCommand()
    )

    item = result.items[0]
    assert item.outcome is EnumJoinJ5Outcome.PROJECTED_ROW_MISSING
    assert item.failure_code == POLICY.projected_row_missing_failure_code
    assert POLICY.projection_read_failed_failure_code != (
        POLICY.projected_row_missing_failure_code
    )
    assert "no projected row at ledger_seq" in item.detail
