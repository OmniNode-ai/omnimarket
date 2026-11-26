# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The bootstrap publishes history once and the relay holds live events behind it (OMN-25630, L12, AC1).

Golden chain: null-sequence history, sequenced history and live events offered while the bootstrap runs end on
one partition with every bootstrap event before every live one, and the topic rebuilds into the table the history
and live rows make. Error chain: a crash in the middle of the bootstrap leaves live events held, and the restarted
relay resumes from what the topic holds. Failing control: a relay that publishes live events at once is caught by
the same ordering check.
"""

from __future__ import annotations

import pytest

from omnimarket.models.model_work_ledger_accepted_row import (
    EnumAcceptedRowSource,
)
from omnimarket.nodes.node_projection_work_ledger.handlers.handler_work_ledger_accepted_row_relay import (
    AcceptedRowRelayError,
    HandlerWorkLedgerAcceptedRowRelay,
)
from omnimarket.nodes.node_projection_work_ledger.models.model_work_ledger_accepted_row_relay import (
    ModelAcceptedRowRelayRequest,
)
from omnimarket.nodes.node_work_ledger_reducer_compute.logic.work_ledger_rebuild import (
    WorkLedgerRebuild,
)
from omnimarket.nodes.node_work_ledger_reducer_compute.models.model_work_ledger_rebuild import (
    ModelRebuildRequest,
)
from tests.unit.nodes.node_projection_work_ledger.accepted_row_support import (
    FakePartition,
    PartitionCrashedError,
    history_event,
    live_event,
    no_live_before_a_bootstrap_event,
)

pytestmark = pytest.mark.unit

# history: three rows with no sequence (legacy_ordinal 1..3), then sequenced rows 1..4 (H = 4); live rows follow.
WATERMARK = 4


def _history() -> tuple:  # type: ignore[type-arg]
    legacy = [history_event(n, ordinal=n) for n in (1, 2, 3)]
    sequenced = [history_event(n, seq=n - 3) for n in (4, 5, 6, 7)]
    return tuple(
        reversed(legacy + sequenced)
    )  # the table hands them back in no useful order


def _live(*pairs: tuple[int, int]) -> tuple:  # type: ignore[type-arg]
    return tuple(live_event(n, seq) for n, seq in pairs)


def test_bootstrap_watermark_puts_history_first_in_the_l3_order_then_live() -> None:
    partition = FakePartition()
    # live events judged after H are offered BEFORE the bootstrap pass and out of order
    live = _live((9, 6), (8, 5))
    result = HandlerWorkLedgerAcceptedRowRelay(partition).handle(
        ModelAcceptedRowRelayRequest(history=_history(), watermark=WATERMARK, live=live)
    )
    assert result.bootstrap_complete
    assert (result.bootstrap_published, result.live_published) == (7, 2)
    events = partition.events
    assert no_live_before_a_bootstrap_event(events)
    # L3 ordering contract: unsequenced rows by legacy_ordinal, then sequenced rows by ledger_seq, then live by seq
    assert [(e.legacy_ordinal, e.ledger_seq) for e in events[:3]] == [
        (1, None),
        (2, None),
        (3, None),
    ]
    assert [e.ledger_seq for e in events[3:7]] == [1, 2, 3, 4]
    assert [e.ledger_seq for e in events[7:]] == [5, 6]
    assert {e.source for e in events[:7]} == {EnumAcceptedRowSource.BOOTSTRAP}
    assert len({e.row_id for e in events}) == len(events) == 9


def test_bootstrap_watermark_rows_written_after_h_are_live_and_not_history() -> None:
    partition = FakePartition()
    # the table read raced a write: sequence 5 is in the history read but above H
    racing = history_event(8, seq=5)
    result = HandlerWorkLedgerAcceptedRowRelay(partition).handle(
        ModelAcceptedRowRelayRequest(
            history=(*_history(), racing),
            watermark=WATERMARK,
            live=_live((8, 5)),
        )
    )
    assert result.above_watermark_history_skipped == 1
    assert [e.source for e in partition.events].count(
        EnumAcceptedRowSource.BOOTSTRAP
    ) == 7
    assert [
        e.ledger_seq for e in partition.events if e.source is EnumAcceptedRowSource.LIVE
    ] == [5]
    assert no_live_before_a_bootstrap_event(partition.events)


def test_bootstrap_restart_in_the_middle_holds_live_and_resumes_without_a_repeat() -> (
    None
):
    partition = FakePartition(crash_after=4)
    request = ModelAcceptedRowRelayRequest(
        history=_history(), watermark=WATERMARK, live=_live((8, 5))
    )
    with pytest.raises(PartitionCrashedError):
        HandlerWorkLedgerAcceptedRowRelay(partition).handle(request)
    # four bootstrap events are on the partition, no live event is
    assert len(partition.events) == 4
    assert {e.source for e in partition.events} == {EnumAcceptedRowSource.BOOTSTRAP}

    # concurrent writes land while the relay is down, then a NEW relay (a restart) runs with a healthy partition
    partition.crash_after = None
    restarted = HandlerWorkLedgerAcceptedRowRelay(partition).handle(
        ModelAcceptedRowRelayRequest(
            history=_history(),
            watermark=WATERMARK,
            live=_live((9, 6), (8, 5), (10, 7)),
        )
    )
    assert restarted.bootstrap_already_on_topic == 4
    assert restarted.bootstrap_published == 3
    assert restarted.bootstrap_complete
    assert restarted.live_published == 3
    events = partition.events
    assert no_live_before_a_bootstrap_event(events)
    assert len({e.row_id for e in events}) == len(events) == 10, (
        "every row once, none repeated by the restart"
    )


def test_bootstrap_restart_live_events_stay_held_until_the_history_is_on_the_partition() -> (
    None
):
    # the history publishes return but the partition does not hold them when read back: the bootstrap is not
    # complete, so the relay publishes no live event and names the one it holds
    partition = FakePartition(lossy=True)
    result = HandlerWorkLedgerAcceptedRowRelay(partition).handle(
        ModelAcceptedRowRelayRequest(
            history=_history(), watermark=WATERMARK, live=_live((8, 5))
        )
    )
    assert not result.bootstrap_complete
    assert result.live_published == 0
    assert result.live_held_row_ids == (live_event(8, 5).row_id,)
    assert partition.events == []


def test_bootstrap_watermark_a_second_pass_publishes_nothing_twice() -> None:
    partition = FakePartition()
    request = ModelAcceptedRowRelayRequest(
        history=_history(), watermark=WATERMARK, live=_live((8, 5))
    )
    relay = HandlerWorkLedgerAcceptedRowRelay(partition)
    relay.handle(request)
    again = relay.handle(request)
    assert (again.bootstrap_published, again.live_published) == (0, 0)
    assert again.live_duplicates_dropped == 1
    assert len(partition.events) == 8


def test_bootstrap_watermark_live_twin_of_an_unsequenced_row_carries_the_sequence() -> (
    None
):
    partition = FakePartition()
    twin = live_event(1, 5)  # the same text as legacy row 1, now judged and sequenced
    HandlerWorkLedgerAcceptedRowRelay(partition).handle(
        ModelAcceptedRowRelayRequest(
            history=_history(), watermark=WATERMARK, live=(twin,)
        )
    )
    assert [e.ledger_seq for e in partition.events if e.row_id == twin.row_id] == [
        None,
        5,
    ]
    # the twin is not a duplicate to drop: the rebuild takes the sequence from it
    rebuilt = WorkLedgerRebuild().rebuild(tuple(partition.events))
    assert {r.row_id: r.ledger_seq for r in rebuilt.rows}[twin.row_id] == 5


def test_bootstrap_restart_topic_rebuilds_the_same_table_as_history_plus_live() -> None:
    partition = FakePartition(crash_after=5)
    request = ModelAcceptedRowRelayRequest(
        history=_history(), watermark=WATERMARK, live=_live((8, 5), (9, 6))
    )
    with pytest.raises(PartitionCrashedError):
        HandlerWorkLedgerAcceptedRowRelay(partition).handle(request)
    partition.crash_after = None
    HandlerWorkLedgerAcceptedRowRelay(partition).handle(request)
    # the table the rows make when folded straight from the same text, with no topic in between
    straight = WorkLedgerRebuild().rebuild(
        tuple(
            sorted(
                (*_history(), *_live((8, 5), (9, 6))),
                key=lambda e: (
                    e.ledger_seq is not None,
                    e.legacy_ordinal or 0,
                    e.ledger_seq or 0,
                ),
            )
        )
    )
    result = WorkLedgerRebuild().handle(
        ModelRebuildRequest(events=tuple(partition.events), live=straight)
    )
    assert result.compared
    assert result.equal, result.differences


def test_bootstrap_failing_control_a_relay_that_skips_the_hold_is_caught() -> None:
    """The ordering check must fail for a partition with a live event ahead of history."""
    partition = FakePartition()
    partition.publish(live_event(8, 5))
    for event in sorted(
        _history(),
        key=lambda e: (
            e.ledger_seq is not None,
            e.legacy_ordinal or 0,
            e.ledger_seq or 0,
        ),
    ):
        partition.publish(event)
    assert not no_live_before_a_bootstrap_event(partition.events)


def test_bootstrap_refuses_an_unjudged_live_row_and_history_marked_live() -> None:
    relay = HandlerWorkLedgerAcceptedRowRelay(FakePartition())
    unsequenced_live = live_event(8, 5).model_copy(update={"ledger_seq": None})
    with pytest.raises(AcceptedRowRelayError, match="never judged"):
        relay.handle(
            ModelAcceptedRowRelayRequest(
                history=_history(), watermark=WATERMARK, live=(unsequenced_live,)
            )
        )
    with pytest.raises(AcceptedRowRelayError, match="not marked source=bootstrap"):
        relay.handle(
            ModelAcceptedRowRelayRequest(
                history=(live_event(8, 5),), watermark=WATERMARK
            )
        )
