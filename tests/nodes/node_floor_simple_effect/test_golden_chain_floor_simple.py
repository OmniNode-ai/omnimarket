# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain of the 17a cycle (OMN-19998): the sources are read, the lab is judged, the simple floor is written.

The error chain file holds the breaks of each thing named here.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.emission_sink import (
    InMemoryEmissionSink,
)
from omnimarket.models.floor_17a import (
    ModelFloorSimple,
    ModelFloorSimpleInput,
    ModelFloorSimpleTickRequest,
    ModelFloorSimpleTickResult,
)
from omnimarket.nodes.node_floor_simple_compute.handlers.handler_floor_simple import (
    HandlerFloorSimple,
)
from omnimarket.nodes.node_floor_simple_effect.handlers.handler_floor_simple_tick import (
    HandlerFloorSimpleTick,
)
from tests.nodes.node_floor_simple_effect.support import (
    NOW,
    OVERLAYS,
    build_home,
    environ,
    fleet,
    lab_alarm_run,
    record,
)

pytestmark = pytest.mark.unit


def ticking() -> Callable[[], datetime]:
    """A clock that moves one second per call, so events have an order."""
    state = {"n": 0}

    def clock() -> datetime:
        state["n"] += 1
        return NOW + timedelta(seconds=state["n"])

    return clock


def run(
    home: Path,
    sink: InMemoryEmissionSink | None = None,
    *,
    env: dict[str, str] | None = None,
    served: Callable[[], set[str] | None] | None = None,
    drill: bool = False,
) -> ModelFloorSimpleTickResult:
    handler = HandlerFloorSimpleTick(
        sink=sink or InMemoryEmissionSink(),
        clock=ticking(),
        served_alarm_ids=served or (lambda: None),
    )
    return handler.run(
        ModelFloorSimpleTickRequest(home=str(home), drill_error=drill),
        environ=env or environ(home),
    )


def written(home: Path) -> ModelFloorSimple:
    path = home / ".claude_scratch/factory-floor/floor-simple.json"
    return ModelFloorSimple.model_validate_json(path.read_text(encoding="utf-8"))


def test_golden_a_healthy_lab_is_drawn_from_its_readings_with_all_18_rows(
    tmp_path: Path,
) -> None:
    home = build_home(tmp_path)
    result = run(home)
    assert result.ok
    doc = written(home)
    assert [p.piece for p in doc.pieces] == list(range(1, 19))
    assert [line.id for line in doc.lab_state] == [
        "hosts",
        "runner_groups",
        "disk",
        "queue_age",
        "deploy_lag",
        "broker_lag",
    ]
    assert all(
        line.state == "WORKING" and line.draw == "green" for line in doc.lab_state
    )
    assert all(line.reading_age_s is not None for line in doc.lab_state)
    assert doc.red_rule.state == "WORKING"
    assert doc.red_rule.red_with_work_under_way == 0
    assert doc.details_href == "detail.html"


def test_golden_the_three_numbers_come_from_reads_a_checker_can_repeat(
    tmp_path: Path,
) -> None:
    home = build_home(tmp_path)
    run(home)
    doc = written(home)
    merges, prs, slots = doc.numbers
    # r#9 (in scope) and r#7 (a landing prerequisite) merged inside the hour; r#10 merged inside it too but is outside
    # the goal, and r#8 merged 90 minutes before the cycle.
    assert (merges.key, merges.value, merges.read) == ("merges", "2", True)
    # Two open pull requests of the goal (r#1 and r#3); r#2 is paused, r#4 and r#6 are unlisted, r#5 is merged.
    assert (prs.key, prs.value) == ("open_prs", "2")
    # 8 + 6 running of 32 + 16 slots; h105's bay is stale and is named, not counted.
    assert (slots.key, slots.value) == ("slots", "14 of 48")
    assert "1 hosts unread" in slots.label
    reads = json.loads(
        (home / ".claude_scratch/factory-floor/floor-simple-reads.json").read_text()
    )
    again = HandlerFloorSimple().handle(
        ModelFloorSimpleInput.model_validate(reads["floor_input"])
    )
    # The customer health line (OMN-22923) is added after this compute from the same piece rows; it is repeated in its own
    # test. The MVP launch block (OMN-23205) reads the producers' runs, not this input, and is tested on its own.
    assert again.model_dump(mode="json") == doc.model_copy(
        update={"health": None, "mvp_launch": None}
    ).model_dump(mode="json")


def test_golden_each_node_emits_a_start_then_a_success_under_one_correlation_id(
    tmp_path: Path,
) -> None:
    sink = InMemoryEmissionSink()
    home = build_home(tmp_path)
    result = run(home, sink)
    chain = [e for e in sink.events if e.correlation_id == result.correlation_id]
    cycle = "node_floor_simple_effect:run_floor_simple_cycle"
    assert (chain[0].node, chain[0].phase) == (cycle, "start")
    assert (chain[-1].node, chain[-1].phase) == (cycle, "success")
    inner = [(e.node, e.phase) for e in chain[1:-1]]
    steps = [
        "node_floor_simple_effect:read_sources",
        "node_floor_simple_compute:compute_lab_state",
        "node_lab_state_alarm_effect:alarm_lab_state",
        "node_floor_emission_compute:compute_floor_receipts",
        "node_floor_simple_compute:compute_floor_simple",
        "node_product_floor_verdict_compute:decide_customer_health",
    ]
    assert inner == [(n, p) for n in steps for p in ("start", "success")]
    assert chain[-1].counters["pieces"] == 18
    assert {e.piece for e in chain} == {17}


def test_golden_the_floor_row_shows_a_golden_and_an_error_event_after_the_first_cycle(
    tmp_path: Path,
) -> None:
    home = build_home(tmp_path)
    run(home)
    row = next(p for p in written(home).pieces if p.piece == 17)
    assert row.golden_age_s is not None
    assert row.error_age_s is not None
    assert row.freshness == "fresh"


def test_golden_a_planted_source_taken_offline_draws_red_and_raises_an_alarm(
    tmp_path: Path,
) -> None:
    home = build_home(
        tmp_path, fleet_doc={"status": "unread", "reason": "ssh host unset"}
    )
    run(home)
    doc = written(home)
    line = next(x for x in doc.lab_state if x.id == "runner_groups")
    assert (line.state, line.draw) == ("UNKNOWN", "red")
    assert "ssh host unset" in line.text
    assert [a.source_id for a in doc.alarms] == ["runner_groups"]
    assert doc.level == "red"
    assert doc.red_rule.state == "WORKING"


def test_golden_a_red_source_with_a_lane_on_it_draws_amber_not_red(
    tmp_path: Path,
) -> None:
    roots = tmp_path / "overlays"
    shutil.copytree(OVERLAYS, roots)
    path = roots / "node_floor_simple_compute" / "overlay.yaml"
    overlay = yaml.safe_load(path.read_text())
    overlay["causes"]["owners"]["lab:runner_groups"] = ["OMN-30001"]
    path.write_text(yaml.safe_dump(overlay), encoding="utf-8")
    home = build_home(
        tmp_path, fleet_doc={"status": "unread", "reason": "ssh host unset"}
    )
    env = environ(home) | {"ONEX_SKILL_OVERLAY_ROOTS": str(roots)}
    run(home, env=env)
    doc = written(home)
    line = next(x for x in doc.lab_state if x.id == "runner_groups")
    assert (line.draw, line.work_under_way) == ("amber", True)
    cause = next(c for c in doc.causes if c.id == "lab:runner_groups")
    assert (cause.state, cause.lanes) == ("amber", ("build-a",))
    assert doc.red_rule.red_with_work_under_way == 0


def test_golden_an_alarm_is_delivered_when_the_served_floor_shows_it(
    tmp_path: Path,
) -> None:
    home = build_home(
        tmp_path, fleet_doc={"status": "unread", "reason": "ssh host unset"}
    )
    run(home)
    first = written(home)
    assert [a.delivery for a in first.alarms] == ["undelivered"]
    assert "alarm:undelivered" in {c.id for c in first.causes}

    def served() -> set[str] | None:
        return {a.alarm_id for a in written(home).alarms}

    run(home, served=served)
    second = written(home)
    assert [a.delivery for a in second.alarms] == ["delivered"]
    assert [a.channel for a in second.alarms] == ["floor_banner"]
    assert "alarm:undelivered" not in {c.id for c in second.causes}


def test_golden_a_source_that_recovers_clears_its_alarm(tmp_path: Path) -> None:
    home = build_home(
        tmp_path, fleet_doc={"status": "unread", "reason": "ssh host unset"}
    )
    run(home)
    assert written(home).alarms
    (home / ".onex_state/runner-fleet/runner-fleet.json").write_text(
        json.dumps(fleet())
    )
    run(home)
    assert written(home).alarms == ()


def test_golden_a_stale_broker_read_is_not_working_and_names_its_age(
    tmp_path: Path,
) -> None:
    home = build_home(tmp_path, alarm_run=lab_alarm_run(finished_minutes_ago=40))
    run(home)
    line = next(x for x in written(home).lab_state if x.id == "broker_lag")
    assert line.state == "NOT WORKING"
    assert line.text.startswith("NOT WORKING: stale: read 40 min ago")


def test_golden_a_host_the_pool_refuses_is_a_failing_host_not_a_missing_one(
    tmp_path: Path,
) -> None:
    rec = record()
    rec["floor"]["machines"][0]["admission"] = {
        "state": "refused",
        "reason": "PARITY-FAILED: plugin-stale",
    }
    home = build_home(tmp_path, rec=rec)
    run(home)
    line = next(x for x in written(home).lab_state if x.id == "hosts")
    assert line.state == "NOT WORKING"
    assert "h051" in line.text
    assert "PARITY-FAILED" in line.text
