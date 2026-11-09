# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC1 of OMN-23018: the value at now minus 1, 7 and 30 days is read from the dated history; a missing point is unread, never zero."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from omnimarket.nodes.node_floor_compare_effect.handlers.adapter_floor_history import (
    prune,
    read_history,
    reading_at,
)
from omnimarket.nodes.node_floor_compare_effect.handlers.adapter_floor_metrics import (
    snapshot_from_floor_simple,
)
from tests.nodes.node_floor_compare_effect.support import (
    NOW,
    RT,
    seed,
    served_floor,
    snap,
)

pytestmark = pytest.mark.unit


def _history() -> tuple:
    return (
        snap(1, {"open_prs": 12, "merges": 4}),
        snap(7, {"open_prs": 20, "merges": 9}),
        snap(30, {"open_prs": 31, "merges": 1}),
    )


@pytest.mark.parametrize(
    ("days", "tolerance", "want"), [(1, 3, 12.0), (7, 12, 20.0), (30, 24, 31.0)]
)
def test_the_value_at_now_minus_1_7_and_30_days_is_read(
    days: int, tolerance: float, want: float
) -> None:
    reading = reading_at(_history(), "open_prs", NOW - timedelta(days=days), tolerance)
    assert (reading.read, reading.value) == (True, want)


def test_a_planted_gap_reads_unread_and_never_zero() -> None:
    gapped = tuple(s for s in _history() if s.at != NOW - timedelta(days=7))
    reading = reading_at(gapped, "open_prs", NOW - timedelta(days=7), 12)
    assert reading.read is False
    assert (
        reading.value is None
    )  # not 0.0: a zero would read as "nothing open a week ago"
    assert "no snapshot within 12 h" in reading.reason


def test_failing_control_the_same_history_without_the_gap_reads_the_value() -> None:
    assert reading_at(_history(), "open_prs", NOW - timedelta(days=7), 12).read is True


def test_a_snapshot_near_the_time_that_did_not_read_the_key_is_unread_not_zero() -> (
    None
):
    history = (snap(7, {"merges": 9}),)
    reading = reading_at(history, "open_prs", NOW - timedelta(days=7), 12)
    assert (reading.read, reading.value) == (False, None)
    assert "none read it" in reading.reason


def test_a_snapshot_outside_the_tolerance_is_not_the_reading() -> None:
    history = (snap(7.5, {"open_prs": 20}),)  # 12 h away, tolerance 6 h
    assert reading_at(history, "open_prs", NOW - timedelta(days=7), 6).read is False


def test_the_closest_snapshot_wins() -> None:
    history = (
        snap(7.2, {"open_prs": 1}),
        snap(7.01, {"open_prs": 2}),
        snap(6.7, {"open_prs": 3}),
    )
    assert reading_at(history, "open_prs", NOW - timedelta(days=7), 12).value == 2.0


def test_a_number_the_floor_did_not_read_is_not_stored_as_zero() -> None:
    doc = served_floor()
    for number in doc["numbers"]:
        if number["key"] == "slots":
            number.update(read=False, value="unread")
    snapshot = snapshot_from_floor_simple(doc, RT, source="tick")
    assert snapshot is not None
    assert "slots_busy" not in snapshot.values
    assert snapshot.values["open_prs"] == 8.0
    assert snapshot.values["merges"] == 2.0


def test_the_published_numbers_are_all_kept() -> None:
    snapshot = snapshot_from_floor_simple(served_floor(), RT, source="tick")
    assert snapshot is not None
    keys = set(snapshot.values)
    assert {"merges", "open_prs", "slots_busy", "red_rows", "alarms_total"} <= keys
    assert any(k.startswith("cause:") for k in keys)
    assert any(k.startswith("stoppage_cost:") for k in keys)
    assert any(k.startswith("alarm:") for k in keys)
    assert any(k.startswith("piece:") for k in keys)
    assert snapshot.values["slots_busy"] == 8.0
    assert snapshot.values["red_rows"] == 28.0
    assert (
        snapshot.values[
            "stoppage_cost:stoppage:hook-refusal:unauthorised-shared-tree-git-mutation"
        ]
        == 144.0
    )


def test_a_line_that_does_not_parse_is_skipped_and_counted(tmp_path: Path) -> None:
    seed(tmp_path, snap(1, {"open_prs": 12}))
    path = tmp_path / RT.history_relpath
    path.write_text(path.read_text() + "{ not json\n", encoding="utf-8")
    snapshots, skipped = read_history(path)
    assert (len(snapshots), skipped) == (1, 1)


def test_the_history_keeps_the_retention_window_and_drops_the_rest() -> None:
    kept = prune(
        (snap(40, {"a": 1}), snap(34, {"a": 2}), snap(0, {"a": 3})),
        NOW,
        RT.retention_days,
    )
    assert [s.values["a"] for s in kept] == [2.0, 3.0]
