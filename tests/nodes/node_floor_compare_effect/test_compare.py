# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC2 of OMN-23018: better and worse lines in plain words, each with the old value, the new value and which direction is better."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_floor_compare_effect.handlers.handler_floor_compare import (
    build_comparison,
    family_of,
)
from tests.nodes.node_floor_compare_effect.support import RT, snap

pytestmark = pytest.mark.unit


def _now(values: dict[str, float]):
    return snap(0, values)


def _window(comparison, days: int):
    return next(w for w in comparison.windows if w.days == days)


def test_the_three_windows_are_one_seven_and_thirty_days() -> None:
    comparison = build_comparison((), _now({"open_prs": 1}), RT)
    assert [w.days for w in comparison.windows] == [1, 7, 30]


def test_fewer_open_pull_requests_is_better_and_fewer_merges_is_worse() -> None:
    history = (snap(1, {"open_prs": 12, "merges": 4}),)
    comparison = build_comparison(history, _now({"open_prs": 8, "merges": 2}), RT)
    day = _window(comparison, 1)
    better = {line.key: line for line in day.better}
    worse = {line.key: line for line in day.worse}
    assert set(better) == {"open_prs"}
    assert set(worse) == {"merges"}
    line = better["open_prs"]
    assert (line.old, line.new, line.better_is) == (12.0, 8.0, "lower_is_better")
    assert "8 open now, was 12 open 1 day ago" in line.words
    assert "Lower is better, so this is better." in line.words
    assert "Higher is better, so this is worse." in worse["merges"].words


def test_failing_control_the_same_move_is_worse_when_the_direction_flips() -> None:
    history = (snap(1, {"open_prs": 8}),)
    comparison = build_comparison(history, _now({"open_prs": 12}), RT)
    assert [ln.key for ln in _window(comparison, 1).worse] == ["open_prs"]
    assert _window(comparison, 1).better == ()


def test_an_unchanged_number_is_counted_same_and_listed_nowhere() -> None:
    comparison = build_comparison(
        (snap(1, {"red_rows": 28}),), _now({"red_rows": 28}), RT
    )
    day = _window(comparison, 1)
    assert (day.same, day.better, day.worse) == (1, (), ())


def test_a_planted_gap_is_an_unread_line_and_never_better_or_worse() -> None:
    history = (
        snap(1, {"open_prs": 12}),
        snap(30, {"open_prs": 31}),
    )  # nothing near 7 days
    comparison = build_comparison(history, _now({"open_prs": 8}), RT)
    week = _window(comparison, 7)
    assert week.better == ()
    assert week.worse == ()
    assert [(ln.key, ln.old, ln.verdict) for ln in week.unread] == [
        ("open_prs", None, "unread")
    ]
    assert "no reading 7 days ago" in week.unread[0].words
    assert _window(comparison, 1).counts["better"] == 1


def test_a_number_read_then_and_not_now_is_unread_not_zero() -> None:
    comparison = build_comparison((snap(1, {"open_prs": 12}),), _now({"merges": 2}), RT)
    line = next(ln for ln in _window(comparison, 1).unread if ln.key == "open_prs")
    assert (line.old, line.new) == (12.0, None)
    assert "no reading now" in line.words


def test_a_rebuild_piece_turning_green_is_better_in_words() -> None:
    history = (snap(7, {"piece:3": 3}),)
    now = _now({"piece:3": 0}).model_copy(
        update={"labels": {"piece:3": "3, Bus, runtime and seams"}}
    )
    line = _window(build_comparison(history, now, RT), 7).better[0]
    assert (line.old_text, line.new_text) == ("red", "green")
    assert line.words.startswith(
        "Rebuild piece 3, Bus, runtime and seams: green now, was red 7 days ago."
    )


def test_a_stoppage_cost_going_up_is_worse() -> None:
    key = "stoppage_cost:stoppage:x"
    history = (snap(1, {key: 100}),)
    line = _window(build_comparison(history, _now({key: 144}), RT), 1).worse[0]
    assert (
        "144 lane-minutes lost now, was 100 lane-minutes lost 1 day ago" in line.words
    )


def test_the_longest_prefix_picks_the_family() -> None:
    assert family_of("alarms_total", RT).prefix == "alarms_total"
    assert family_of("alarm:deploy_agent", RT).prefix == "alarm:"
    assert family_of("unknown_key", RT) is None
