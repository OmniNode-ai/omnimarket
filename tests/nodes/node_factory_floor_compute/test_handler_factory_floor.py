# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fixture tests of the factory floor compute: an andon-stopped plan, a plan with no manifest, the floor."""

from __future__ import annotations

import ast
import json
from datetime import datetime
from pathlib import Path

import pytest

from omnimarket.models.model_factory_floor_input import (
    ModelFloorLedgerRow,
    ModelFloorLinearIssue,
    ModelFloorMachineDecl,
    ModelFloorPoolReading,
    ModelFloorStoppage,
)
from omnimarket.nodes.node_factory_floor_compute.handlers.handler_factory_floor import (
    HandlerFactoryFloor,
    compute_factory_floor,
)
from omnimarket.nodes.node_factory_floor_compute.models.model_factory_floor_output import (
    ModelFactoryFloorResult,
    ModelFloorLine,
)
from tests.nodes.node_factory_floor_compute.support import (
    PLAN_PATH,
    advertisement,
    ago,
    config,
    manifest_of,
    merge,
    pr,
    request,
    row,
    table_plan,
    task,
    wave_plan,
)

pytestmark = pytest.mark.unit

HANDLER_SOURCE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_factory_floor_compute/handlers/handler_factory_floor.py"
)


def _line(result: ModelFactoryFloorResult, index: int = 0) -> ModelFloorLine:
    return result.lines[index]


def _states(line: ModelFloorLine) -> dict[str, str]:
    return {item.id: item.state for st in line.stations for item in st.items}


def _station_states(line: ModelFloorLine) -> dict[str, str]:
    return {st.id: st.state for st in line.stations}


# --------------------------------------------------------------------------------------- purity


def test_purity_handler_reads_no_clock_file_network_or_environment() -> None:
    tree = ast.parse(HANDLER_SOURCE.read_text(encoding="utf-8"))
    banned_modules = {
        "os",
        "sys",
        "subprocess",
        "socket",
        "pathlib",
        "time",
        "random",
        "requests",
        "httpx",
        "urllib",
        "yaml",
        "json",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert not {a.name.split(".")[0] for a in node.names} & banned_modules
        if isinstance(node, ast.ImportFrom) and node.module:
            assert node.module.split(".")[0] not in banned_modules
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in {"open", "print", "input", "exec", "eval"}
        if isinstance(node, ast.Attribute):
            assert node.attr not in {"now", "utcnow", "today", "environ", "read_text"}


# ------------------------------------------------------------------------------------- manifest


def test_manifest_line_record_has_pill_percent_stations_and_feeders() -> None:
    plan = wave_plan()
    result = compute_factory_floor(
        request(
            plans=(plan,),
            prs=(pr("kb#1", "MERGED", title="W0.1 reuse verification"),),
            ledger=(row("CLAIM", 10, task="W0.2"),),
        )
    )
    line = _line(result)
    assert line.line_id == "2026-10-31-wave-plan"
    assert line.name == "Orchestration alignment, the wave plan (tasks)"
    assert line.plan_path == PLAN_PATH
    assert line.source == "manifest"
    assert line.status_pill == "building"
    assert line.percent_built == 33  # W0.1 done of W0.1, W0.2, P1 (gates are not parts)
    assert _states(line) == {
        "W0.1": "done",
        "W0.2": "active",
        "G0": "wait",
        "P1": "wait",
        "G1a": "wait",
    }
    assert _station_states(line) == {"0": "active", "1a": "wait"}
    station0, station1a = line.stations
    assert (station0.name, station0.feeders) == ("Wave 0", ())
    assert (station1a.name, station1a.feeders) == ("Wave 1a", ("0",))
    notes = {item.id: item.note for item in station0.items}
    assert notes["W0.1"] == "PRs merged"
    assert notes["W0.2"] == "claim lane-a"
    assert notes["G0"] == "waits for W0.2"
    assert line.andon.flag is False
    assert line.andon.reason == ""


def test_manifest_gate_pass_opens_next_station_to_prep() -> None:
    result = compute_factory_floor(
        request(
            prs=(
                pr("kb#1", "MERGED", title="W0.1 a"),
                pr("kb#2", "MERGED", head_ref="w0.2-branch W0.2"),
            ),
            ledger=(
                row("STATUS", 5, state="gate", subject="0", verdict="pass"),
                row("STATUS", 4, state="gate", subject="elsewhere", verdict="fail"),
            ),
        )
    )
    line = _line(result)
    assert _states(line) == {
        "W0.1": "done",
        "W0.2": "done",
        "G0": "done",
        "P1": "prep",
        "G1a": "wait",
    }
    assert _station_states(line) == {"0": "done", "1a": "prep"}
    assert line.status_pill == "building"
    assert line.percent_built == 66


def test_manifest_all_done_is_a_finished_line() -> None:
    plan = wave_plan()
    result = compute_factory_floor(
        request(
            plans=(plan,),
            prs=(
                pr("kb#1", "MERGED", title="W0.1"),
                pr("kb#2", "MERGED", title="W0.2"),
                pr("kb#3", "MERGED", title="P1"),
            ),
            ledger=(
                row("STATUS", 9, state="gate", subject="0", verdict="pass"),
                row("STATUS", 8, state="gate", subject="1a", verdict="pass"),
            ),
        )
    )
    line = _line(result)
    assert (line.status_pill, line.percent_built) == ("done", 100)
    assert set(_station_states(line).values()) == {"done"}


def test_manifest_evidence_from_terminal_linear_open_pr_and_stale_claim() -> None:
    plan = wave_plan().model_copy(
        update={
            "manifest": manifest_of(wave_plan()).model_copy(
                update={
                    "tasks": (
                        task("A", "x", tickets=("OMN-1",)),
                        task("B", "x", tickets=("OMN-2",)),
                        task("C", "x", prs=("repo#3",)),
                        task("D", "x"),
                        task("E", "x"),
                    )
                }
            )
        }
    )
    result = compute_factory_floor(
        request(
            plans=(plan,),
            prs=(pr("repo#3", "OPEN"),),
            ledger=(
                row(
                    "CLAIM", 300, lane="old", task="D"
                ),  # a claim whose lane went quiet
                row("CLAIM", 90, lane="x", task="E"),
                row("TERMINAL", 80, lane="x", task="E", outcome="blocked"),
            ),
            linear=(
                ModelFloorLinearIssue(
                    id="OMN-1", status="Done", status_type="completed"
                ),
                ModelFloorLinearIssue(
                    id="OMN-2", status="In Progress", status_type="started"
                ),
            ),
        )
    )
    states = _states(_line(result))
    notes = {i.id: i.note for st in _line(result).stations for i in st.items}
    assert states == {
        "A": "done",
        "B": "active",
        "C": "active",
        "D": "prep",
        "E": "wait",
    }
    assert notes["A"] == "Linear done"
    assert notes["B"] == "Linear in progress"
    assert notes["C"] == "PR repo#3 open"
    assert notes["E"] == "lane x ended blocked"


# ------------------------------------------------------------------------- andon or fallback


def _andon_ledger() -> tuple[ModelFloorLedgerRow, ...]:
    return (
        row("CLAIM", 200, lane="retro", task="W0.2"),
        row("TERMINAL", 150, lane="retro", task="W0.2", outcome="blocked"),
        row(
            "HOLD",
            120,
            lane="retro",
            row_id="hold-andon-1",
            reason="andon:gate:0:W0.2",
            tickets=("OMN-100",),
            until=None,
        ),
        row("STATUS", 100, lane="retro", state="gate", subject="0", verdict="fail"),
    )


def test_andon_or_fallback_andon_stopped_plan_flags_reason_and_stops_station() -> None:
    result = compute_factory_floor(
        request(
            prs=(pr("kb#1", "MERGED", title="W0.1"),),
            ledger=_andon_ledger(),
        )
    )
    line = _line(result)
    assert line.status_pill == "stopped"
    assert line.andon.flag is True
    assert line.andon.reason == "HOLD hold-andon-1: andon:gate:0:W0.2 (+1 more)"
    assert line.andon.causes == (
        "HOLD hold-andon-1: andon:gate:0:W0.2",
        "GATE G0 failed",
    )
    states = _states(line)
    assert states["W0.2"] == "stop"
    assert states["G0"] == "stop"
    assert _station_states(line) == {"0": "stop", "1a": "wait"}
    note = {i.id: i.note for st in line.stations for i in st.items}
    assert note["W0.2"] == "andon hold hold-andon-1: andon:gate:0:W0.2"
    assert note["G0"].startswith("gate failed ")


def test_andon_or_fallback_a_released_expired_or_order_hold_is_not_an_andon() -> None:
    base = (row("STATUS", 100, lane="r", state="gate", subject="0", verdict="pass"),)
    hold = row(
        "HOLD",
        120,
        lane="r",
        row_id="h1",
        reason="andon:gate:0:W0.2",
        tickets=("OMN-100",),
    )
    released = compute_factory_floor(
        request(ledger=(*base, hold, row("RELEASE", 60, re="h1")))
    )
    assert _line(released).andon.flag is False
    expired = compute_factory_floor(
        request(
            ledger=(*base, hold.model_copy(update={"until": ago(1)})),
        )
    )
    assert _line(expired).andon.flag is False
    order = hold.model_copy(update={"row_id": "order-1", "reason": "W0.2 after PR 5"})
    held = compute_factory_floor(request(ledger=(*base, order)))
    line = _line(held)
    assert line.andon.flag is False
    assert _states(line)["W0.2"] == "wait"
    assert {i.id: i.note for st in line.stations for i in st.items}["W0.2"] == (
        "held order-1: W0.2 after PR 5"
    )


def test_andon_or_fallback_a_partial_release_by_pr_keeps_the_hold_in_force() -> None:
    hold = row(
        "HOLD",
        120,
        lane="r",
        row_id="h1",
        reason="andon:factory",
        tickets=("OMN-100",),
        prs=("kb#9",),
    )
    result = compute_factory_floor(
        request(ledger=(hold, row("RELEASE", 60, re="h1", prs=("kb#9",))))
    )
    assert _line(result).andon.flag is True


def test_andon_or_fallback_config_not_handler_defines_the_andon_word() -> None:
    hold = row(
        "HOLD", 5, lane="r", row_id="stopline-7", tickets=("OMN-100",), reason="x"
    )
    default = compute_factory_floor(request(ledger=(hold,)))
    assert _line(default).andon.flag is False
    renamed = config().model_copy(update={"andon_markers": ("stopline",)})
    result = compute_factory_floor(request(ledger=(hold,), cfg=renamed))
    assert _line(result).andon.flag is True


def test_andon_or_fallback_plan_with_no_manifest_uses_the_plans_today_task_table() -> (
    None
):
    result = compute_factory_floor(request(plans=(table_plan(),)))
    line = _line(result)
    assert line.source == "plan-task-table"
    assert line.line_id == "2026-10-01-old-plan"
    assert line.name == "Old plan"
    assert len(line.stations) == 1
    only = line.stations[0]
    assert (only.id, only.name, only.feeders) == (
        "tasks",
        "Task table (no manifest)",
        (),
    )
    assert [(i.id, i.state) for i in only.items] == [
        ("T1", "done"),
        ("T2", "active"),
        ("T3", "prep"),
    ]
    assert only.items[1].note == "PR open, red: CI Summary red"
    assert only.state == "active"
    assert line.percent_built == 33
    assert line.status_pill == "building"
    assert line.andon.flag is False


def test_andon_or_fallback_table_plan_row_stops_on_an_andon_hold_of_its_ticket() -> (
    None
):
    hold = row("HOLD", 5, lane="r", row_id="andon-9", tickets=("OMN-9",), reason="red")
    result = compute_factory_floor(request(plans=(table_plan(),), ledger=(hold,)))
    line = _line(result)
    assert _states(line)["T3"] == "stop"
    assert line.status_pill == "stopped"
    assert line.andon.reason == "HOLD andon-9: red"


def test_andon_or_fallback_an_empty_manifest_falls_back_to_the_table() -> None:
    plan = table_plan().model_copy(
        update={"manifest": manifest_of(wave_plan()).model_copy(update={"tasks": ()})}
    )
    assert _line(compute_factory_floor(request(plans=(plan,)))).source == (
        "plan-task-table"
    )


def test_selection_keeps_only_matching_plans_and_draws_an_unmatched_token_stopped() -> (
    None
):
    result = compute_factory_floor(
        request(
            plans=(table_plan(), wave_plan()), selection=("wave-plan", "nonexistent")
        )
    )
    assert [line.line_id for line in result.lines] == [
        "2026-10-31-wave-plan",
        "nonexistent",
    ]
    assert result.lines[1].source == "undrawn"


# --------------------------------------------------------------------------------------- floor


def pool_reading(
    host: str, *, cap: int, read_at: datetime | None = None
) -> ModelFloorPoolReading:
    return ModelFloorPoolReading(
        host=host, read_at=read_at or ago(2), cores=80, load1=1.0, cap=cap
    )


def test_floor_bays_count_the_lanes_placed_against_the_cap_and_mark_an_unread_host_stale() -> (
    None
):
    result = compute_factory_floor(
        request(
            machines=(
                ModelFloorMachineDecl(host="h051", label="h051"),
                ModelFloorMachineDecl(host="h105", label="h105"),
                ModelFloorMachineDecl(host="h201", label="h201"),
            ),
            pool=(
                pool_reading("h051", cap=24),
                pool_reading("h105", cap=2, read_at=ago(90)),
            ),
            ledger=tuple(
                row("CLAIM", 5, f"lane-{n}", host="h051", tickets=(f"OMN-{n}",))
                for n in range(20)
            ),
            # The bus advertisement says one unit slot and none running: it is not the lane count.
            placement=(advertisement("h051", running=0, slots=1, age_seconds=500),),
        )
    )
    assert [
        (b.host, b.slots, b.running, b.free, b.stale) for b in result.floor.bays
    ] == [
        ("h051", 24, 20, 4, False),
        ("h105", 2, 0, 0, True),
        ("h201", 0, 0, 0, True),
    ]


def test_floor_shipped_and_waiting_prs_attach_to_their_line() -> None:
    result = compute_factory_floor(
        request(
            merges=(
                merge("kb#1", 30, title="W0.1 done", tickets=("OMN-100",)),
                merge("kb#2", 10, title="unrelated"),
                merge("kb#3", 60 * 30, title="too old"),
            ),
            prs=(
                pr("kb#5", "OPEN", title="W0.2 work"),
                pr("kb#6", "OPEN", draft=True, tickets=("OMN-100",)),
                pr("kb#7", "OPEN", red=True, tickets=("OMN-100",)),
                pr("other#1", "OPEN", title="not on any line"),
                pr("kb#1", "MERGED", tickets=("OMN-100",)),
            ),
        )
    )
    assert [(s.pr, s.line_id) for s in result.floor.shipped] == [
        ("kb#2", ""),
        ("kb#1", "2026-10-31-wave-plan"),
    ]
    assert result.floor.shipped[1].detail == "merged 2026-10-31T11:30:00Z"
    assert [(w.pr, w.detail) for w in result.floor.waiting] == [
        ("kb#5", "waiting to land"),
        ("kb#6", "draft"),
        ("kb#7", "ci red"),
    ]
    assert {w.line_id for w in result.floor.waiting} == {"2026-10-31-wave-plan"}


def test_floor_stoppages_from_the_friction_report_ranked_and_limited() -> None:
    limited = config().model_copy(update={"stoppage_limit": 2})
    report = tuple(
        ModelFloorStoppage(name=n, occurrences=o, cost_minutes=c)
        for n, o, c in (("b", 2, 10.0), ("a", 5, 10.0), ("c", 1, 99.0), ("d", 1, 1.0))
    )
    result = compute_factory_floor(request(stoppages=report, cfg=limited))
    assert [s.name for s in result.floor.stoppages] == ["c", "a"]
    assert result.sources.stoppage_report is True
    assert compute_factory_floor(request()).floor.stoppages == ()


def test_floor_sources_record_what_was_read() -> None:
    result = compute_factory_floor(
        request(
            ledger=(row("CLAIM", 20, task="W0.2"), row("STATUS", 7, state="x")),
            linear=(ModelFloorLinearIssue(id="OMN-1"),),
            placement=(advertisement("h1", running=0, slots=1),),
        )
    )
    assert result.sources.watcher_last_tick == "2026-10-31T11:57:00Z"
    assert result.sources.ledger_rows == 2
    assert result.sources.ledger_newest_row == "2026-10-31T11:53:00Z"
    assert (result.sources.linear_issues, result.sources.placement_hosts) == (1, 1)
    assert result.generated_at == "2026-10-31T12:00:00Z"
    assert result.schema_id == config().schema_id


# ------------------------------------------------------------------------- determinism or cli


def test_determinism_or_cli_same_input_gives_identical_json() -> None:
    req = request(
        plans=(wave_plan(), table_plan()),
        prs=(pr("kb#1", "MERGED", title="W0.1"),),
        ledger=_andon_ledger(),
        placement=(advertisement("h201", running=1, slots=4),),
    )
    first = HandlerFactoryFloor().handle(req).model_dump_json()
    second = HandlerFactoryFloor().handle(req).model_dump_json()
    assert first == second
    json.loads(first)


def test_determinism_or_cli_a_dependency_cycle_or_unknown_dependency_never_counts_as_done() -> (
    None
):
    plan = wave_plan().model_copy(
        update={
            "manifest": manifest_of(wave_plan()).model_copy(
                update={
                    "tasks": (
                        task("A", "x", depends_on=("B",)),
                        task("B", "x", depends_on=("A",)),
                        task("C", "y", depends_on=("MISSING",)),
                    )
                }
            )
        }
    )
    line = _line(compute_factory_floor(request(plans=(plan,))))
    assert _states(line) == {"A": "wait", "B": "wait", "C": "wait"}
