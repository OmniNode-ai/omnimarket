# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain of the 17a cycle (OMN-19998): each break is injected, named, and must turn the reading.

A source that cannot be read is drawn as unread and is never zero; a failure is typed and emitted; the colour rule
catches a builder that gets it wrong; and a missing overlay refuses before anything is emitted.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnimarket.handlers.emission_sink import (
    InMemoryEmissionSink,
)
from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.models.floor_17a import (
    ModelFloorCause,
    ModelFloorSimpleInput,
    ModelFloorSimpleTickRequest,
)
from omnimarket.nodes.node_floor_simple_compute.handlers.handler_floor_simple import (
    HandlerFloorSimple,
    check_red_rule,
)
from omnimarket.nodes.node_floor_simple_effect.handlers.handler_floor_simple_tick import (
    HandlerFloorSimpleTick,
)
from tests.nodes.node_floor_simple_effect.support import build_home, environ
from tests.nodes.node_floor_simple_effect.test_golden_chain_floor_simple import (
    run,
    written,
)

pytestmark = pytest.mark.unit


def test_error_the_drill_emits_a_typed_failure_and_writes_no_floor(
    tmp_path: Path,
) -> None:
    sink = InMemoryEmissionSink()
    home = build_home(tmp_path)
    result = run(home, sink, drill=True)
    assert not result.ok
    assert result.failure_code == "drill_completed"
    failures = [e for e in sink.events if e.phase == "failure"]
    inner = next(e for e in failures if e.node.endswith(":read_sources"))
    assert inner.failure_code == "record_payload_invalid"
    assert inner.drill is True
    assert not (home / ".claude_scratch/factory-floor/floor-simple.json").exists()


def test_error_a_floor_with_no_error_receipt_runs_its_own_drill_first(
    tmp_path: Path,
) -> None:
    sink = InMemoryEmissionSink()
    run(build_home(tmp_path), sink)
    drills = [e for e in sink.events if e.drill]
    assert [e.phase for e in drills] == ["start", "failure"]
    assert len({e.correlation_id for e in drills}) == 1


def test_error_an_unreadable_record_is_drawn_unread_and_never_as_zero(
    tmp_path: Path,
) -> None:
    home = build_home(tmp_path)
    (home / ".claude_scratch/factory-floor/factory-floor.json").write_text("{ not json")
    assert run(home).ok
    doc = written(home)
    slots = next(n for n in doc.numbers if n.key == "slots")
    assert (slots.value, slots.read) == ("unread", False)
    assert "source:floor_record" in {c.id for c in doc.causes}
    hosts = next(x for x in doc.lab_state if x.id == "hosts")
    assert (hosts.state, hosts.draw) == ("UNKNOWN", "red")


def test_error_a_missing_watcher_makes_two_numbers_and_the_queue_unread(
    tmp_path: Path,
) -> None:
    home = build_home(tmp_path)
    (home / ".claude_scratch/pr-watcher/state.json").unlink()
    run(home)
    doc = written(home)
    by_key = {n.key: n for n in doc.numbers}
    assert by_key["merges"].value == by_key["open_prs"].value == "unread"
    queue = next(x for x in doc.lab_state if x.id == "queue_age")
    assert queue.state == "UNKNOWN"
    assert "source:pull_request_watcher" in {c.id for c in doc.causes}


def test_error_a_stale_watcher_is_not_drawn_as_a_current_number(tmp_path: Path) -> None:
    from tests.nodes.node_floor_simple_effect.support import NOW, iso, watcher

    old = watcher(last_tick=iso(NOW.replace(hour=12)))
    home = build_home(tmp_path, watch=old)
    run(home)
    merges = next(n for n in written(home).numbers if n.key == "merges")
    assert (merges.value, merges.read) == ("unread", False)


def test_error_an_unexpected_failure_is_typed_emitted_and_leaves_the_old_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = build_home(tmp_path)
    run(home)
    before = (home / ".claude_scratch/factory-floor/floor-simple.json").read_bytes()

    def broken(self: HandlerFloorSimple, request: ModelFloorSimpleInput) -> None:
        raise ValueError("planted break")

    monkeypatch.setattr(HandlerFloorSimple, "handle", broken)
    sink = InMemoryEmissionSink()
    result = run(home, sink)
    assert (result.ok, result.failure_code) == (False, "unexpected_ValueError")
    failed = [(e.node, e.failure_code) for e in sink.events if e.phase == "failure"]
    assert (
        "node_floor_simple_compute:compute_floor_simple",
        "unexpected_ValueError",
    ) in failed
    assert (
        home / ".claude_scratch/factory-floor/floor-simple.json"
    ).read_bytes() == before


def test_error_a_builder_that_ignores_the_lanes_fails_the_colour_rule_on_a_planted_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = build_home(tmp_path)
    run(home)
    good = written(home)
    assert good.red_rule.red_with_work_under_way == 0
    reads = json.loads(
        (home / ".claude_scratch/factory-floor/floor-simple-reads.json").read_text()
    )
    floor_input = ModelFloorSimpleInput.model_validate(reads["floor_input"])

    # The planted break: a builder that draws every cause red whatever the lanes are doing.
    monkeypatch.setattr(
        HandlerFloorSimple,
        "_causes",
        staticmethod(
            lambda cands, _r: tuple(
                ModelFloorCause.model_validate(
                    {
                        "id": c.id,
                        "title": c.title,
                        "detail": c.detail,
                        "size": c.size,
                        "size_unit": c.size_unit,
                        "state": "red",
                        "work_under_way": False,
                        "needs_operator": False,
                        "lanes": (),
                        "tickets": c.tickets,
                        "owners": c.owners,
                        "prs": c.prs,
                    }
                )
                for c in cands
            )
        ),
    )
    bad = HandlerFloorSimple().handle(floor_input)
    monkeypatch.undo()
    reading = check_red_rule(bad, floor_input.lanes)
    assert reading.state == "NOT WORKING"
    assert reading.red_with_work_under_way >= 1
    assert any("work under way" in v or "while" in v for v in reading.violations)
    assert bad.red_rule.state == "NOT WORKING"


def test_error_a_missing_overlay_refuses_before_anything_is_emitted(
    tmp_path: Path,
) -> None:
    sink = InMemoryEmissionSink()
    home = build_home(tmp_path)
    handler = HandlerFloorSimpleTick(sink=sink)
    with pytest.raises(NodeOverlayError, match="node_floor_simple_compute"):
        handler.run(
            ModelFloorSimpleTickRequest(home=str(home)),
            environ={"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "nowhere")},
        )
    assert sink.events == []


def test_error_no_home_refuses_naming_the_variable(tmp_path: Path) -> None:
    with pytest.raises(NodeOverlayError, match="OMNI_HOME is not set"):
        HandlerFloorSimpleTick(sink=InMemoryEmissionSink()).run(
            ModelFloorSimpleTickRequest(), environ=environ(tmp_path)
        )
