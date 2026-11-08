# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lists behind the floor's numbers are read in the same pass as the numbers (OMN-23018).

The first page's numbers open a panel of the pull requests, lanes and rows they count. The publisher builds each list in the
loop that counts, so the lines add up to the number by construction; these tests hold that against the watcher state and the
floor record, through a whole cycle, and plant a break to show the document says so.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from omnimarket.models.floor_17a import ModelFloorSimple, ModelGoalScope
from omnimarket.nodes.node_floor_simple_effect.handlers.sources import (
    NO_ADMISSIONS,
    read_watcher,
    record_bays,
)
from tests.nodes.node_floor_simple_effect.support import (
    NOW,
    build_home,
    iso,
    record,
    watcher,
)
from tests.nodes.node_floor_simple_effect.test_golden_chain_floor_simple import (
    run,
    written,
)

pytestmark = pytest.mark.unit

SCOPE = ModelGoalScope(
    ruling="a test goal", in_scope_prs=("r#1", "r#3", "r#9", "r#10", "r#7")
)


def reading(home: Path):  # type: ignore[no-untyped-def]
    got = read_watcher(
        home / ".claude_scratch/pr-watcher/state.json",
        NOW,
        excluded_classes=("held-excluded",),
        queue_classes=("green-unarmed", "queue-stuck"),
        merge_window_minutes=60,
        scope=SCOPE,
        admissions=NO_ADMISSIONS,
    )
    assert got is not None
    return got


def test_golden_the_watcher_lists_exactly_the_pull_requests_it_counts(
    tmp_path: Path,
) -> None:
    got = reading(build_home(tmp_path))
    assert got.open_prs == len(got.open_items) >= 1
    assert got.merges_in_window == len(got.merge_items) >= 1
    assert {i.key for i in got.open_items} <= {"r#1", "r#3"}
    assert {i.key for i in got.merge_items} == {"r#9", "r#10", "r#7"}


def test_golden_each_open_pull_request_carries_its_class_and_the_watchers_reason(
    tmp_path: Path,
) -> None:
    doc = watcher()
    doc["prs"]["r#1"]["reason"] = "red: CI Summary, Lint"
    doc["prs"]["r#1"]["ci"] = {
        "red": ["CI Summary", "Lint"],
        "detail": [],
    }
    doc["prs"]["r#1"]["facts"]["created_at"] = iso(NOW - timedelta(hours=5))
    got = reading(build_home(tmp_path, watch=doc))
    item = next(i for i in got.open_items if i.key == "r#1")
    assert (item.cls, item.repo, item.number) == ("red", "r", 1)
    assert item.reason == "red: CI Summary, Lint"
    assert item.red_checks == ("CI Summary", "Lint")
    assert item.at == NOW - timedelta(hours=5)


def test_error_a_held_excluded_or_out_of_scope_pull_request_is_not_listed(
    tmp_path: Path,
) -> None:
    got = reading(build_home(tmp_path))
    assert "r#4" not in {
        i.key for i in got.open_items
    }  # held-excluded and outside the goal
    assert "r#8" not in {
        i.key for i in got.merge_items
    }  # merged 90 minutes ago, outside the window


def test_golden_the_bays_behind_the_slots_number_are_kept_whole() -> None:
    bays = record_bays(record())
    assert [(b.host, b.running, b.stale) for b in bays] == [
        ("h051", 8, False),
        ("h053", 6, False),
        ("h105", 0, True),
    ]


def test_golden_a_whole_cycle_writes_panels_whose_lines_add_up_to_each_number(
    tmp_path: Path,
) -> None:
    home = build_home(tmp_path)
    assert run(home).ok
    doc: ModelFloorSimple = written(home)
    for key in ("merges", "open_prs", "slots"):
        panel = doc.details[f"number:{key}"]
        number = next(n for n in doc.numbers if n.key == key)
        assert number.read
        assert panel.total == int(number.value.split(" ")[0]), key
        assert panel.reconciles, (key, panel.total, panel.counted)
    red = doc.details["number:red_rows"]
    assert red.total == doc.red_rule.red_rows
    assert red.reconciles


def test_golden_a_whole_cycle_opens_a_panel_for_every_cause_lab_line_and_piece(
    tmp_path: Path,
) -> None:
    home = build_home(tmp_path)
    assert run(home).ok
    doc = written(home)
    expected = (
        {f"cause:{c.id}" for c in doc.causes}
        | {f"lab:{line.id}" for line in doc.lab_state}
        | {f"piece:{p.piece}" for p in doc.pieces}
    )
    assert expected <= set(doc.details)


def test_golden_the_slots_panel_matches_the_record_bays_summed_by_a_second_reader(
    tmp_path: Path,
) -> None:
    rec = record()
    rec["floor"]["lanes"][0]["host"] = "h051"
    rec["floor"]["lanes"][0]["started_at"] = iso(NOW - timedelta(minutes=12))
    home = build_home(tmp_path, rec=rec)
    assert run(home).ok
    panel = written(home).details["number:slots"]
    busy = sum(b["running"] for b in rec["floor"]["bays"] if not b["stale"])
    assert panel.total == busy == 14
    assert sum(i.count for i in panel.sections[0].items) == busy
    lane = next(i for i in panel.sections[0].items if i.title == "landing-L1")
    assert lane.group == "h051: 8 of 32 slots busy"
    assert lane.at == NOW - timedelta(minutes=12)


def test_failing_control_a_bay_the_publisher_does_not_read_breaks_the_sum(
    tmp_path: Path,
) -> None:
    # A second reader that counts the stale bay too gets a different number than the panel's total: the control that
    # shows the panel is held to the record, not to itself.
    rec = record()
    rec["floor"]["bays"][2]["running"] = 2
    home = build_home(tmp_path, rec=rec)
    assert run(home).ok
    panel = written(home).details["number:slots"]
    everything = sum(b["running"] for b in rec["floor"]["bays"])
    assert panel.total == everything - 2
    assert panel.total != everything
    assert panel.reconciles
