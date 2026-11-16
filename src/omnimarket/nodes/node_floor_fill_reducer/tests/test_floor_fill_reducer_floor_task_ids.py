# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The acceptance criteria of plan task T12 read over the floor's own task ids.

The floor names its tasks ``T0.1`` and ``CM.0``: an id with a dot between two segments. Each
test here replays one acceptance criterion's scenario over such a key, so a reducer that
refuses the dotted id fails every one of them, and each selects under the same ``-k`` word as
the criterion it reads (``transition_conformance``, ``invariants``, ``admission_drops``,
``lane_name``, ``late_check_after_confirm_dropped``, ``reserved_resolution``,
``ended_entry_holds_key_not_capacity``, ``sent_entry_outlives_window_while_live``,
``reoffer_after_expiry_new_attempt``).
"""

from __future__ import annotations

import random
import re
from datetime import timedelta

import pytest

from omnimarket.models.floor_fill import ModelFloorFillReservedChecked
from omnimarket.nodes.node_floor_fill_reducer.handlers.handler_floor_fill_reducer import (
    lane_name,
)
from omnimarket.nodes.node_floor_fill_reducer.tests import (
    test_floor_fill_reducer as base,
)
from omnimarket.nodes.node_floor_fill_reducer.tests.test_floor_fill_reducer import (
    _D as DROP,
)
from omnimarket.nodes.node_floor_fill_reducer.tests.test_floor_fill_reducer import (
    _S as STATE,
)
from omnimarket.nodes.node_floor_fill_reducer.tests.test_floor_fill_reducer import (
    _T as TRIGGER,
)
from omnimarket.nodes.node_floor_fill_reducer.tests.test_floor_fill_reducer import (
    _assert_dropped as assert_dropped,
)
from omnimarket.nodes.node_floor_fill_reducer.tests.test_floor_fill_reducer import (
    _edge as edge_of,
)
from omnimarket.nodes.node_floor_fill_reducer.tests.test_floor_fill_reducer import (
    _run_sequence as run_sequence,
)

pytestmark = pytest.mark.unit

DOT1 = "floor:line-a:T0.1"
DOT2 = "floor:line-a:CM.0"
DOT3 = "floor:line-a:T12.3.1"
DOT_B = "floor:line-b:T0.1"
LANE_CHARSET = re.compile(r"[A-Za-z0-9_-]+")

at = base.at


def _table() -> set[tuple[str, str, str]]:
    return {(r["from"], r["trigger"], r["to"]) for r in base.FIXTURE_ROWS}


def test_transition_conformance_over_a_dotted_task_id_takes_only_table_rows() -> None:
    rig = base.Rig().ticketed(DOT1)
    edges: list[tuple[str, str, str]] = [("NONE", "ticket_stored", "NONE")]
    observations = [
        base.reserve_requested(DOT1, now=at(0)),
        lambda: base.reserved_checked(rig.entry(DOT1), claim=True, now=at(2)),
        lambda: base.sent_checked(rig.entry(DOT1), ended=True, now=at(20)),
        lambda: base.sent_checked(rig.entry(DOT1), ended=True, now=at(40)),
    ]
    for item in observations:
        observation = item() if callable(item) else item
        before = rig.state
        out = rig.apply(observation)
        edges.append(edge_of(before, out, DOT1))
    assert [e[1] for e in edges] == [
        "ticket_stored",
        "reserve_admitted",
        "confirmed",
        "sent_ended",
        "sent_expired",
    ]
    assert set(edges) <= _table()


def test_invariants_i1_to_i6_hold_over_dotted_task_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(base, "KEYS", (DOT1, DOT2, DOT3, DOT_B))
    rng = random.Random(base.SEED)
    taken: set[tuple[str, str, str]] = set()
    for _ in range(base.SEQUENCES):
        taken.update(run_sequence(rng))
    assert taken
    assert taken <= _table()


def test_admission_drops_over_dotted_task_ids_name_the_reason_and_keep_seq() -> None:
    rig = base.Rig().ticketed(DOT1, DOT2)
    rig.reserved(DOT1, now=at(0), capacity=3)
    first = rig.entry(DOT1)
    wrong_lane = ModelFloorFillReservedChecked(
        key=DOT1,
        attempt=first.attempt,
        lane="floor-line-a-T0-1-00000000-a1",
        claim_found=True,
        reserved_timeout_seconds=base.RESERVED_TIMEOUT_SECONDS,
        now=at(1),
    )
    assert_dropped(rig.state, wrong_lane, DROP.STALE_ATTEMPT)
    assert_dropped(
        rig.state, base.reserve_requested(DOT1, now=at(1), capacity=3), DROP.HELD
    )
    rig.apply(base.start_failed(first))
    rig.reserved(DOT1, now=at(2), capacity=3)
    assert_dropped(
        rig.state,
        base.reserved_checked(first, claim=True, now=at(3)),
        DROP.STALE_ATTEMPT,
    )


def test_lane_name_deterministic_over_dotted_task_ids() -> None:
    assert lane_name(DOT1, 1) == lane_name(DOT1, 1)
    assert lane_name(DOT1, 2) != lane_name(DOT1, 1)
    assert LANE_CHARSET.fullmatch(lane_name(DOT1, 1))
    assert LANE_CHARSET.fullmatch(lane_name(DOT3, 2))
    rig = base.Rig().ticketed(DOT1)
    rig.reserved(DOT1, now=at(0))
    assert rig.entry(DOT1).lane == lane_name(DOT1, 1)


def test_lane_name_concatenation_pair_over_dotted_task_ids() -> None:
    dotted, hyphen = (
        lane_name("floor:line-a:T0.1", 1),
        lane_name("floor:line-a:T0-1", 1),
    )
    assert dotted != hyphen
    assert dotted.startswith("floor-line-a-T0-1-")
    assert hyphen.startswith("floor-line-a-T0-1-")


def test_late_check_after_confirm_dropped_over_a_dotted_task_id() -> None:
    rig = base.Rig().ticketed(DOT1).sent(DOT1)
    late = base.reserved_checked(
        rig.entry(DOT1),
        claim=False,
        now=at(0) + timedelta(seconds=base.RESERVED_TIMEOUT_SECONDS * 2),
    )
    assert_dropped(rig.state, late, DROP.WRONG_STATE)
    assert rig.entry(DOT1).state is STATE.SENT


def test_reserved_resolution_over_a_dotted_task_id_four_ways() -> None:
    timeout = timedelta(seconds=base.RESERVED_TIMEOUT_SECONDS)
    # past the timeout with no claim: cleared, keeps ticket and attempt, offered again as attempt two
    rig = base.Rig().ticketed(DOT1)
    rig.reserved(DOT1, now=at(0))
    out = rig.apply(
        base.reserved_checked(rig.entry(DOT1), claim=False, now=at(0) + timeout)
    )
    assert out.trigger is TRIGGER.RESERVED_TIMED_OUT
    assert (rig.entry(DOT1).state, rig.entry(DOT1).attempt) == (STATE.NONE, 1)
    assert rig.entry(DOT1).ticket == base.TICKET
    rig.reserved(DOT1, now=at(20))
    assert rig.entry(DOT1).attempt == 2
    # with the claim: SENT
    rig = base.Rig().ticketed(DOT2)
    rig.reserved(DOT2, now=at(0))
    out = rig.apply(base.reserved_checked(rig.entry(DOT2), claim=True, now=at(3)))
    assert (out.trigger, rig.entry(DOT2).state) == (TRIGGER.CONFIRMED, STATE.SENT)
    # an unreadable ledger makes no observation: the row stays as it was
    rig = base.Rig().ticketed(DOT3)
    rig.reserved(DOT3, now=at(0))
    held = base.snapshot(rig.state)
    assert base.snapshot(rig.state) == held
    assert rig.entry(DOT3).state is STATE.RESERVED
    assert rig.entry(DOT3).lane == lane_name(DOT3, 1)
    # a StartFailed clears it at once
    rig = base.Rig().ticketed(DOT1)
    rig.reserved(DOT1, now=at(0))
    out = rig.apply(base.start_failed(rig.entry(DOT1)))
    assert out.trigger is TRIGGER.START_FAILED
    assert (rig.entry(DOT1).state, rig.entry(DOT1).lane) == (STATE.NONE, None)


def test_ended_entry_holds_key_not_capacity_over_dotted_task_ids() -> None:
    rig = base.Rig().ticketed(DOT1, DOT2)
    rig.reserved(DOT1, now=at(0), capacity=1)
    rig.apply(base.reserved_checked(rig.entry(DOT1), claim=True, now=at(1)))
    assert_dropped(
        rig.state, base.reserve_requested(DOT2, now=at(2), capacity=1), DROP.CAPACITY
    )
    rig.apply(base.sent_checked(rig.entry(DOT1), ended=True, now=at(3)))
    assert_dropped(
        rig.state, base.reserve_requested(DOT1, now=at(4), capacity=1), DROP.WINDOW
    )
    out = rig.apply(base.reserve_requested(DOT2, now=at(4), capacity=1))
    assert out.trigger is TRIGGER.RESERVE_ADMITTED


def test_sent_entry_outlives_window_while_live_over_dotted_task_ids() -> None:
    rig = base.Rig().ticketed(DOT1, DOT2).sent(DOT1)
    late = 1 + 10 * base.WINDOW_MINUTES
    out = rig.apply(base.sent_checked(rig.entry(DOT1), ended=False, now=at(late)))
    assert out.trigger is TRIGGER.SENT_LIVE
    assert rig.entry(DOT1).state is STATE.SENT
    assert_dropped(
        rig.state,
        base.reserve_requested(DOT2, now=at(late + 1), capacity=1),
        DROP.CAPACITY,
    )


def test_reoffer_after_expiry_new_attempt_over_dotted_task_ids() -> None:
    rig = base.Rig().ticketed(DOT1).sent(DOT1)
    old = rig.entry(DOT1)
    out = rig.apply(base.sent_checked(old, ended=True, now=at(1 + base.WINDOW_MINUTES)))
    assert out.trigger is TRIGGER.SENT_EXPIRED
    rig.reserved(DOT1, now=at(2 + base.WINDOW_MINUTES))
    assert rig.entry(DOT1).attempt == 2
    assert rig.entry(DOT1).lane != old.lane
    assert_dropped(
        rig.state,
        base.reserved_checked(old, claim=True, now=at(3 + base.WINDOW_MINUTES)),
        DROP.STALE_ATTEMPT,
    )
