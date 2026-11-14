# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The reducer folds censuses into per-group empty-since times and names the groups past the bound (OMN-23992)."""

from __future__ import annotations

import asyncio

import pytest

from omnimarket.nodes.node_consumer_group_state_reducer.handlers.handler_group_state import (
    GroupStateTransitionError,
    HandlerGroupState,
    fold,
)
from omnimarket.nodes.node_consumer_group_state_reducer.models import (
    ModelGroupCensusEvent,
    ModelGroupState,
    ModelGroupStateRecord,
    ModelGroupStateReduceCommand,
    decision_digest,
)
from tests.nodes.node_consumer_group_state_reducer.support import (
    DRIFT,
    DUE_A,
    DUE_B,
    LANE,
    PROTECTED_LOOKALIKE,
    STABLE,
    census,
    fold_all,
    overlay,
)

pytestmark = pytest.mark.unit

BOUND = overlay().empty_since_bound_seconds


def by_name(record: ModelGroupStateRecord) -> dict[str, ModelGroupState]:
    return {g.name: g for g in record.groups}


def test_golden_chain_names_a_group_empty_past_the_bound() -> None:
    first = fold_all([census({DUE_A: "EMPTY", STABLE: "STABLE"}, seconds=0)])
    assert first.reap_due == ()
    second = fold_all([census({DUE_A: "EMPTY", STABLE: "STABLE"}, seconds=300)], first)
    assert second.reap_due == ()
    third = fold_all(
        [census({DUE_A: "EMPTY", STABLE: "STABLE"}, seconds=BOUND + 100)], second
    )
    assert third.reap_due == (DUE_A,)
    assert third.decided_by == "reducer"
    state = by_name(third)[DUE_A]
    assert state.empty_since == first.groups[0].empty_since
    assert by_name(third)[STABLE].empty_since is None
    assert third.decision_digest == decision_digest(
        LANE,
        third.census_correlation_id,
        (DUE_A,),
        {DUE_A: state.empty_since},
        third.updated_at,
    )


def test_a_first_census_never_names_a_group_while_the_bound_is_positive() -> None:
    record = fold_all([census({DUE_A: "EMPTY"}, seconds=BOUND * 10)])
    assert record.reap_due == ()


def test_a_group_that_leaves_empty_and_returns_starts_its_clock_over() -> None:
    record = fold_all(
        [
            census({DUE_A: "EMPTY"}, seconds=0),
            census({DUE_A: "STABLE"}, seconds=BOUND),
            census({DUE_A: "EMPTY"}, seconds=BOUND + 10),
            census({DUE_A: "EMPTY"}, seconds=BOUND + 20),
        ]
    )
    assert record.reap_due == ()
    assert (
        by_name(record)[DUE_A].empty_since.timestamp()
        == census({DUE_A: "EMPTY"}, seconds=BOUND + 10).observed_at.timestamp()
    )


def test_a_group_missing_from_the_census_is_dropped() -> None:
    record = fold_all(
        [census({DUE_A: "EMPTY", DUE_B: "EMPTY"}), census({DUE_B: "EMPTY"}, seconds=5)]
    )
    assert [g.name for g in record.groups] == [DUE_B]


def test_a_protected_name_and_an_unlisted_name_are_never_due() -> None:
    later = BOUND + 1
    record = fold_all(
        [
            census({DRIFT: "EMPTY", STABLE: "EMPTY", PROTECTED_LOOKALIKE: "EMPTY"}),
            census(
                {DRIFT: "EMPTY", STABLE: "EMPTY", PROTECTED_LOOKALIKE: "EMPTY"},
                seconds=later,
            ),
        ]
    )
    assert record.reap_due == ()


def test_the_overlay_cap_limits_the_names_in_one_decision() -> None:
    capped = overlay().model_copy(update={"max_reap_per_decision": 1})
    events = [
        census({DUE_A: "EMPTY", DUE_B: "EMPTY"}, seconds=0),
        census({DUE_A: "EMPTY", DUE_B: "EMPTY"}, seconds=BOUND),
    ]
    assert fold_all(events, bound=capped).reap_due == (DUE_A,)


def test_error_chain_an_empty_census_is_refused() -> None:
    with pytest.raises(ValueError, match="groups"):
        ModelGroupCensusEvent(
            correlation_id=census({DUE_A: "EMPTY"}).correlation_id,
            bus_lane=LANE,
            observed_at=census({DUE_A: "EMPTY"}).observed_at,
            groups={},
        )


def test_error_chain_another_bus_lane_and_an_older_census_are_refused() -> None:
    first = fold_all([census({DUE_A: "EMPTY"}, seconds=100)])
    with pytest.raises(GroupStateTransitionError, match="another bus lane"):
        fold(first, census({DUE_A: "EMPTY"}, seconds=200, lane="lane-b"), overlay())
    with pytest.raises(GroupStateTransitionError, match="out of order"):
        fold(first, census({DUE_A: "EMPTY"}, seconds=50), overlay())


def test_handler_emits_the_record_as_the_projection() -> None:
    command = ModelGroupStateReduceCommand(event=census({DUE_A: "EMPTY"}))
    output = asyncio.run(HandlerGroupState(overlay()).handle(command))
    assert output.projections[0] == fold(None, command.event, overlay())


def naive_due(events: list[ModelGroupCensusEvent], bound: int) -> tuple[str, ...]:
    """The control: a fold that ignores the prior record and counts from the census clock alone."""
    last = events[-1]
    return tuple(
        sorted(
            name
            for name, state in last.groups.items()
            if state == "EMPTY" and last.observed_at.timestamp() >= bound
        )
    )


def test_control_naive_fold_names_a_group_the_real_fold_holds_back() -> None:
    """A group first seen empty in the last census is named by the naive fold and not by the real one."""
    events = [
        census({DUE_A: "STABLE"}, seconds=0),
        census({DUE_A: "EMPTY"}, seconds=BOUND + 5),
    ]
    real = fold_all(events).reap_due
    naive = naive_due(events, BOUND)
    assert naive == (DUE_A,)
    assert real == ()
    assert real != naive


LAG_BAR = overlay().lag_bar_messages
PROJECTION = "example.projection.delegation"
PROJECTION_B = "example.projection.savings"


def test_a_group_past_the_lag_bar_is_named_lagging_and_its_lag_is_held() -> None:
    event = census({PROJECTION: "STABLE", PROJECTION_B: "STABLE"}, seconds=0)
    event = event.model_copy(
        update={"lag": {PROJECTION: LAG_BAR + 1, PROJECTION_B: LAG_BAR}}
    )
    record = fold_all([event])
    assert record.lagging == (PROJECTION,)
    assert by_name(record)[PROJECTION].lag == LAG_BAR + 1
    assert by_name(record)[PROJECTION_B].lag == LAG_BAR


def test_a_group_whose_lag_was_not_read_is_never_named_lagging() -> None:
    record = fold_all([census({PROJECTION: "STABLE"}, seconds=0)])
    assert record.lagging == ()
    assert by_name(record)[PROJECTION].lag is None


def test_lag_is_read_fresh_at_each_census_not_carried_over() -> None:
    first = census({PROJECTION: "STABLE"}, seconds=0).model_copy(
        update={"lag": {PROJECTION: LAG_BAR * 10}}
    )
    second = census({PROJECTION: "STABLE"}, seconds=60).model_copy(
        update={"lag": {PROJECTION: 3}}
    )
    record = fold_all([first, second])
    assert record.lagging == ()
    assert by_name(record)[PROJECTION].lag == 3


def test_lag_does_not_change_the_reap_decision() -> None:
    plain = census({DUE_A: "EMPTY"}, seconds=BOUND + 1)
    lagged = plain.model_copy(update={"lag": {DUE_A: 1}})
    start = census({DUE_A: "EMPTY"}, seconds=0)
    a = fold_all([plain], fold_all([start]))
    b = fold_all([lagged], fold_all([start]))
    assert a.reap_due == b.reap_due == (DUE_A,)
