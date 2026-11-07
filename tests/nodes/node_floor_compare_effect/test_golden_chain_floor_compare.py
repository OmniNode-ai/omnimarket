# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain events of the comparison cycle (OMN-23018): each failure is a named injected break and turns the reading."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnimarket.handlers.emission_sink import InMemoryEmissionSink
from omnimarket.nodes.node_floor_compare_effect.handlers.handler_floor_compare import (
    HandlerFloorCompare,
)
from omnimarket.nodes.node_floor_compare_effect.handlers.handler_floor_history import (
    read_history,
)
from omnimarket.nodes.node_floor_compare_effect.models.model_floor_compare import (
    ModelFloorCompareRequest,
    ModelFloorComparison,
)
from tests.nodes.node_floor_compare_effect.support import (
    NOW,
    RT,
    build_home,
    seed,
    served_floor,
    snap,
)

pytestmark = pytest.mark.unit


def run(home: Path, sink: InMemoryEmissionSink, **kw):
    return HandlerFloorCompare(sink=sink, clock=lambda: NOW).run(
        ModelFloorCompareRequest(home=str(home), now=NOW, **kw)
    )


def written(home: Path) -> ModelFloorComparison:
    return ModelFloorComparison.model_validate_json(
        (home / RT.output_relpath).read_text()
    )


def test_golden_a_cycle_emits_start_then_success_and_writes_the_comparison(
    tmp_path: Path,
) -> None:
    sink = InMemoryEmissionSink()
    home = build_home(tmp_path)
    seed(home, snap(1, {"open_prs": 12, "merges": 4}), snap(7, {"open_prs": 20}))
    result = run(home, sink)
    assert result.ok
    assert result.recorded
    assert [e.phase for e in sink.events] == ["start", "success"]
    assert len({e.correlation_id for e in sink.events}) == 1
    assert sink.events[0].node == RT.emission_node
    assert sink.events[1].counters["history_points"] == 3
    doc = written(home)
    day, week, month = doc.windows
    assert [ln.key for ln in day.better] == ["open_prs"]
    assert [ln.key for ln in week.better] == ["open_prs"]
    assert month.better == ()
    assert month.worse == ()
    assert any(ln.key == "open_prs" for ln in month.unread)


def test_golden_a_second_tick_inside_the_gap_is_not_stored_again(
    tmp_path: Path,
) -> None:
    home = build_home(tmp_path)
    assert run(home, InMemoryEmissionSink()).recorded
    again = run(home, InMemoryEmissionSink())
    assert again.ok
    assert not again.recorded
    assert len(read_history(home / RT.history_relpath)[0]) == 1


def test_golden_dated_copies_are_backfilled_once(tmp_path: Path) -> None:
    home = build_home(tmp_path)
    copies = home / ".claude_scratch/factory-floor/history"
    copies.mkdir(parents=True)
    old = served_floor(NOW.replace(day=6))
    for number in old["numbers"]:
        if number["key"] == "open_prs":
            number["value"] = "12"
    (copies / "floor-simple-20261106T183000Z.json").write_text(json.dumps(old))
    (copies / "floor-simple-bad.json").write_text("{ not json")
    first = run(home, InMemoryEmissionSink())
    assert first.backfilled == 1
    second = run(home, InMemoryEmissionSink())
    assert second.backfilled == 0
    day = written(home).windows[0]
    line = next(ln for ln in day.better if ln.key == "open_prs")
    assert (line.old, line.new) == (12.0, 8.0)


def test_error_the_drill_emits_a_typed_failure_and_writes_nothing(
    tmp_path: Path,
) -> None:
    sink = InMemoryEmissionSink()
    home = build_home(tmp_path)
    result = run(home, sink, drill_error=True)
    assert not result.ok
    assert result.failure_code == "simple_payload_invalid"
    assert [e.phase for e in sink.events] == ["start", "failure"]
    assert sink.events[1].failure_code == "simple_payload_invalid"
    assert sink.events[1].drill
    assert not (home / RT.output_relpath).exists()
    assert not (home / RT.history_relpath).exists()


def test_error_an_unreadable_floor_records_no_zero_and_emits_a_typed_failure(
    tmp_path: Path,
) -> None:
    sink = InMemoryEmissionSink()
    home = build_home(tmp_path)
    (home / RT.simple_relpath).write_text("{ not json")
    result = run(home, sink)
    assert not result.ok
    assert sink.events[-1].phase == "failure"
    assert not (home / RT.history_relpath).exists()


def test_error_a_planted_gap_reaches_the_written_comparison_as_unread(
    tmp_path: Path,
) -> None:
    home = build_home(tmp_path)
    seed(
        home, snap(1, {"open_prs": 12})
    )  # a day ago is read; 7 and 30 days ago are the gap
    run(home, InMemoryEmissionSink())
    doc = written(home)
    assert [w.counts["unread"] > 0 for w in doc.windows] == [True, True, True]
    for window in doc.windows[1:]:
        assert window.better == ()
        assert window.worse == ()
        assert all(ln.old is None for ln in window.unread if ln.key == "open_prs")


def test_error_a_missing_home_refuses_before_anything_is_emitted() -> None:
    sink = InMemoryEmissionSink()
    result = HandlerFloorCompare(sink=sink, clock=lambda: NOW).run(
        ModelFloorCompareRequest(), environ={}
    )
    assert not result.ok
    assert result.failure_code == "home_not_set"
    assert sink.events == []


def test_error_an_interrupted_cycle_closes_its_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sink = InMemoryEmissionSink()
    home = build_home(tmp_path)

    def boom(*_a, **_k):
        raise KeyboardInterrupt

    monkeypatch.setattr(
        "omnimarket.nodes.node_floor_compare_effect.handlers.handler_floor_compare.build_comparison",
        boom,
    )
    with pytest.raises(KeyboardInterrupt):
        run(home, sink)
    assert [e.phase for e in sink.events] == ["start", "failure"]
    assert sink.events[1].failure_code == "cycle_interrupted"
