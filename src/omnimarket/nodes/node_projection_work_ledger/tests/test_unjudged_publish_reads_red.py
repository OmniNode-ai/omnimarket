# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC3 of OMN-23152: projected rows with no sequence read 0 over the window, and an unjudged publish reads RED.

Golden chain: only judged rows (each with a sequence) reach the projection, so the reading is WORKING
at zero. Error chain: one row published with no sequence turns the reading RED and names its emitter
by source and lane. The window is the contract's; a row older than it is not counted. The named
break is the plant itself: the same projection with one unjudged publish added.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from omnimarket.nodes.node_projection_work_ledger.handlers.work_ledger_unjudged_reading import (
    InMemoryUnjudgedRowReader,
    ProjectedRowFact,
    load_unjudged_config,
    read_unjudged,
)
from omnimarket.nodes.node_projection_work_ledger.models.model_work_ledger_unjudged_reading import (
    EnumUnjudgedReadingState,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
LEDGER = "rolling-work-ledger"


def _row(
    seq: int | None,
    *,
    source: str = "onex-ledger",
    lane: str | None = "judge",
    age: timedelta = timedelta(hours=1),
) -> ProjectedRowFact:
    return ProjectedRowFact(
        ledger_id=LEDGER,
        source=source,
        row_lane=lane,
        ledger_seq=seq,
        projected_at=NOW - age,
    )


def test_unjudged_publish_reads_red_golden_zero_unjudged_rows_reads_working() -> None:
    config = load_unjudged_config()
    assert config.window_seconds == 86400
    judged = [_row(seq) for seq in range(190460, 190470)]
    reading = read_unjudged(
        InMemoryUnjudgedRowReader(judged), config, ledger_id=LEDGER, now=NOW
    )
    assert reading.state is EnumUnjudgedReadingState.WORKING
    assert reading.unjudged_count == 0
    assert reading.emitters == ()


def test_unjudged_publish_reads_red_a_planted_unjudged_publish_turns_it_red() -> None:
    config = load_unjudged_config()
    judged = [_row(seq) for seq in range(190460, 190470)]
    planted = _row(None, source="hook-emit", lane="subagent-stop")
    reading = read_unjudged(
        InMemoryUnjudgedRowReader([*judged, planted]), config, ledger_id=LEDGER, now=NOW
    )
    assert reading.state is EnumUnjudgedReadingState.RED
    assert reading.unjudged_count == 1
    (emitter,) = reading.emitters
    assert (emitter.source, emitter.row_lane, emitter.count) == (
        "hook-emit",
        "subagent-stop",
        1,
    )


def test_unjudged_publish_reads_red_names_each_emitter_most_rows_first() -> None:
    config = load_unjudged_config()
    rows = [
        _row(None, source="friction-rollup", lane="rollup"),
        _row(None, source="hook-emit", lane="subagent-stop"),
        _row(None, source="hook-emit", lane="subagent-stop"),
    ]
    reading = read_unjudged(
        InMemoryUnjudgedRowReader(rows), config, ledger_id=LEDGER, now=NOW
    )
    assert [(e.source, e.count) for e in reading.emitters] == [
        ("hook-emit", 2),
        ("friction-rollup", 1),
    ]
    assert reading.unjudged_count == 3


def test_unjudged_publish_reads_red_ignores_rows_older_than_the_window() -> None:
    config = load_unjudged_config()
    old = _row(None, age=timedelta(seconds=config.window_seconds + 1))
    inside = _row(None, age=timedelta(seconds=config.window_seconds - 1))
    reader = InMemoryUnjudgedRowReader([old])
    assert (
        read_unjudged(reader, config, ledger_id=LEDGER, now=NOW).state
        is EnumUnjudgedReadingState.WORKING
    )
    reader = InMemoryUnjudgedRowReader([old, inside])
    assert read_unjudged(reader, config, ledger_id=LEDGER, now=NOW).unjudged_count == 1


def test_unjudged_publish_reads_red_other_ledgers_are_not_counted() -> None:
    config = load_unjudged_config()
    other = ProjectedRowFact("another-ledger", "x", None, None, NOW)
    reading = read_unjudged(
        InMemoryUnjudgedRowReader([other]), config, ledger_id=LEDGER, now=NOW
    )
    assert reading.state is EnumUnjudgedReadingState.WORKING
