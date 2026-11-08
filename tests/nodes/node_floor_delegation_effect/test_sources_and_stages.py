# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The readers and the two stages of the delegation tracker, against a registry home built in a temporary directory (OMN-23081)."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from omnimarket.nodes.node_floor_delegation_effect.handlers.adapter_delegation_sources import (
    read_lanes,
    read_mirror,
    read_scenario,
    read_watcher,
    ticket_of,
)
from omnimarket.nodes.node_floor_delegation_effect.handlers.handler_floor_delegation import (
    HandlerFloorDelegation,
)
from omnimarket.nodes.node_floor_delegation_effect.models.model_floor_delegation import (
    ModelFloorDelegationRequest,
)

from .support import (
    NOW,
    RT,
    FakeRunner,
    missing_workflow_answer,
    runs_answer,
    watcher_pr,
    write_home,
)

URL = "https://github.com/OmniNode-ai"


def _stage(home: Path, stage: str, runner: FakeRunner | None = None):  # type: ignore[no-untyped-def]
    handler = HandlerFloorDelegation(RT, runner or FakeRunner({}), clock=lambda: NOW)
    return handler.handle(ModelFloorDelegationRequest(stage=stage, home=str(home)))  # type: ignore[arg-type]


def _all_green() -> FakeRunner:
    return FakeRunner(
        {
            d.workflow: (0, runs_answer(("completed", "success", 10)), "")
            for d in RT.scenarios
        }
    )


def test_the_mirror_gives_the_title_the_plan_task_and_the_start_condition(
    tmp_path: Path,
) -> None:
    home = write_home(
        tmp_path, tickets={23162: {"start_when": "A3a ([OMN-23161](x)) has merged"}}
    )
    found, source = read_mirror_with_refs(home)
    t = next(m for m in found if m.id == "OMN-23162")
    assert t.plan_task == "A1"
    assert t.start_when == "A3a ([OMN-23161](x)) has merged"
    assert source.ok
    assert "23 of 23" in source.detail


def read_mirror_with_refs(home: Path):  # type: ignore[no-untyped-def]
    from omnimarket.nodes.node_floor_delegation_effect.handlers.adapter_delegation_sources import (
        read_mirror_with_references,
    )

    return read_mirror_with_references(home / RT.ticket_mirror_relpath, RT)


def test_a_ticket_missing_from_the_mirror_is_named(tmp_path: Path) -> None:
    home = write_home(tmp_path)
    (home / RT.ticket_mirror_relpath / "OMN-23170.json").unlink()
    _, source = read_mirror_with_refs(home)
    assert not source.ok
    assert "OMN-23170" in source.detail
    assert read_mirror(home / RT.ticket_mirror_relpath, ["OMN-23170"])[1].endswith(
        "OMN-23170"
    )


def test_a_pull_request_belongs_to_the_first_ticket_in_its_title_else_its_branch() -> (
    None
):
    wanted = {"OMN-23161", "OMN-23162"}
    assert ticket_of("feat(OMN-23161): x, see OMN-23162", "", wanted) == "OMN-23161"
    assert (
        ticket_of("Vendor a migration", "claude/omn-23162-vendor", wanted)
        == "OMN-23162"
    )
    assert ticket_of("feat(OMN-99999): other", "", wanted) == ""
    assert ticket_of("no ticket", "main", wanted) == ""


def test_the_watcher_gives_state_and_checks_and_drops_other_pull_requests(
    tmp_path: Path,
) -> None:
    home = write_home(
        tmp_path,
        prs={
            "omnimarket#1": watcher_pr(23161, number=1, verdict="RED"),
            "omnimarket#2": watcher_pr(
                23162, state="MERGED", number=2, verdict="GREEN"
            ),
            "omnimarket#3": watcher_pr(23163, number=3, verdict=None),
            "omnimarket#4": watcher_pr(70000, number=4),
            "omnimarket#5": watcher_pr(
                23164, number=5, title="Vendor", head_ref="omn-23164-x"
            ),
        },
    )
    prs, source = read_watcher(
        home / RT.watcher_relpath,
        {f"OMN-{n}" for n in range(23161, 23184)},
        URL,
        5400,
        NOW,
    )
    by = {p.ref: p for p in prs}
    assert set(by) == {"omnimarket#1", "omnimarket#2", "omnimarket#3", "omnimarket#5"}
    assert (by["omnimarket#1"].state, by["omnimarket#1"].ci) == ("open", "red")
    assert by["omnimarket#1"].ci_note.startswith("red")
    assert (by["omnimarket#2"].state, by["omnimarket#2"].ci) == ("merged", "green")
    assert by["omnimarket#3"].ci == "unread"
    assert by["omnimarket#5"].ticket == "OMN-23164"
    assert by["omnimarket#1"].url == f"{URL}/omnimarket/pull/1"
    assert source.ok


def test_a_stale_watcher_is_not_ok(tmp_path: Path) -> None:
    home = write_home(tmp_path, watcher_tick=NOW - timedelta(hours=3))
    _, source = read_watcher(home / RT.watcher_relpath, set(), URL, 5400, NOW)
    assert not source.ok
    assert "limit" in source.detail


def test_a_missing_watcher_names_the_reason(tmp_path: Path) -> None:
    prs, source = read_watcher(tmp_path / "none.json", set(), URL, 5400, NOW)
    assert prs == ()
    assert not source.ok
    assert "none.json" in source.detail


def test_lanes_come_from_the_floor_record(tmp_path: Path) -> None:
    home = write_home(
        tmp_path,
        lanes=[
            {
                "lane": "dlg-1",
                "ticket": "OMN-23172",
                "host": "h053",
                "liveness": "alive",
                "stalled": False,
            },
            {"lane": "no-ticket", "ticket": "", "host": "h1", "liveness": "alive"},
        ],
    )
    lanes, source = read_lanes(home / RT.record_relpath, 900, NOW)
    assert [(lane.lane, lane.host) for lane in lanes] == [("dlg-1", "h053")]
    assert source.ok


def test_scenario_read_parses_runs_and_maps_a_404_to_not_built() -> None:
    decl = RT.scenarios[0]
    ok = FakeRunner(
        {
            decl.workflow: (
                0,
                runs_answer(("completed", "success", 5), ("completed", "failure", 65)),
                "",
            )
        }
    )
    got = read_scenario(decl, Path("onex"), RT, ok, NOW)
    assert got.read == "read"
    assert [r.conclusion for r in got.runs] == [
        "success",
        "failure",
    ]
    missing = FakeRunner({decl.workflow: (0, missing_workflow_answer(), "")})
    assert read_scenario(decl, Path("onex"), RT, missing, NOW).read == "no_workflow"
    broken = FakeRunner({decl.workflow: (124, "", "timed out after 25 s")})
    got = read_scenario(decl, Path("onex"), RT, broken, NOW)
    assert got.read == "unread"
    assert "timed out" in got.reason


def test_scenario_read_passes_the_event_and_limit_from_the_contract() -> None:
    seen: list[list[str]] = []

    def capture(argv, timeout):  # type: ignore[no-untyped-def]
        seen.append(list(argv))
        return 0, runs_answer(), ""

    read_scenario(RT.scenarios[0], Path("onex"), RT, capture, NOW)
    argv = seen[0]
    assert argv[argv.index("--event") + 1] == RT.scenario_event
    assert argv[argv.index("--limit") + 1] == str(RT.scenario_run_limit)
    assert argv[3] == RT.scenarios[0].repo


# ---- the two stages end to end ----------------------------------------------------------------------------------------------


def test_stages_end_to_end_all_green_is_shippable(tmp_path: Path) -> None:
    home = write_home(tmp_path)
    assert _stage(home, "scenarios", _all_green()).ok
    result = _stage(home, "build")
    assert result.ok
    assert result.shippable is True
    doc = json.loads((home / RT.output_relpath).read_text())
    assert doc["shippable"] is True
    assert len(doc["scenarios"]) == 9
    assert len(doc["tickets"]) == 23
    assert doc["schema_id"] == "omnimarket.floor_delegation.v1"
    assert doc["counts"]["tickets_total"] == 23


def test_control_stages_end_to_end_one_red_scenario_is_not_shippable(
    tmp_path: Path,
) -> None:
    home = write_home(tmp_path)
    answers = {
        d.workflow: (0, runs_answer(("completed", "success", 10)), "")
        for d in RT.scenarios
    }
    answers[RT.scenarios[6].workflow] = (
        0,
        runs_answer(("completed", "failure", 10)),
        "",
    )
    _stage(home, "scenarios", FakeRunner(answers))
    result = _stage(home, "build")
    assert result.shippable is False
    doc = json.loads((home / RT.output_relpath).read_text())
    assert doc["blocking"] == ["R7"]


def test_build_without_the_scenario_stage_is_not_shippable_and_says_why(
    tmp_path: Path,
) -> None:
    home = write_home(tmp_path)
    result = _stage(home, "build")
    assert result.shippable is False
    doc = json.loads((home / RT.output_relpath).read_text())
    assert doc["counts"]["scenarios_unread"] == 9
    scenario_source = next(s for s in doc["sources"] if s["id"] == "scenario_reads")
    assert not scenario_source["ok"]


def test_the_stage_reads_the_least_recently_read_first_and_keeps_fresh_reads(
    tmp_path: Path,
) -> None:
    home = write_home(tmp_path)
    first = _all_green()
    _stage(home, "scenarios", first)
    assert len(first.read) == 9
    again = _all_green()
    _stage(home, "scenarios", again)
    assert again.read == []  # all read within the refresh limit


def test_the_stage_deadline_stops_new_reads_and_keeps_the_previous_ones(
    tmp_path: Path,
) -> None:
    home = write_home(tmp_path)
    ticks = iter(range(0, 100000, 30))
    handler = HandlerFloorDelegation(
        RT,
        _all_green(),
        clock=lambda: NOW + timedelta(seconds=next(ticks)),
    )
    handler.handle(ModelFloorDelegationRequest(stage="scenarios", home=str(home)))
    kept = json.loads((home / RT.scenario_reads_relpath).read_text())
    assert 0 < len(kept["reads"]) < 9


def test_a_stale_watcher_makes_the_tickets_unread_in_the_served_document(
    tmp_path: Path,
) -> None:
    home = write_home(tmp_path, watcher_tick=NOW - timedelta(hours=5))
    _stage(home, "build")
    doc = json.loads((home / RT.output_relpath).read_text())
    assert doc["counts"]["groups"][-1]["id"] == "unread"
    assert doc["counts"]["groups"][-1]["count"] == 23


def test_the_stage_without_a_home_refuses_by_name(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv(RT.home_env, raising=False)
    result = HandlerFloorDelegation(RT, FakeRunner({}), clock=lambda: NOW).handle(
        ModelFloorDelegationRequest(stage="build")
    )
    assert not result.ok
    assert RT.home_env in result.detail
