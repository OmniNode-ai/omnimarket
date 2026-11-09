# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A scenario read that times out is answered from the cached run list, not drawn unread (OMN-23176).

The R5 scenario read timed out at the contract's 25 s bound and the floor drew it "Unread". The bound stays; the reader now
answers from the run list it already holds: a read that fails keeps the previous good read while that read is younger than
``scenario_reads_max_age_seconds``, and a workflow known to be missing is not asked about again until
``scenario_missing_refresh_seconds`` has passed.
"""

from __future__ import annotations

from pathlib import Path

from omnimarket.nodes.node_floor_delegation_effect.handlers.adapter_delegation_sources import (
    read_scenarios,
)
from omnimarket.nodes.node_floor_delegation_effect.models.model_floor_delegation import (
    ModelScenarioRead,
    ModelScenarioReads,
)

from .support import NOW, RT, FakeRunner, iso, read, run, runs_answer, write_home

DECL = RT.scenarios[0]
OTHERS = [d for d in RT.scenarios if d.id != DECL.id]


def _seed(home: Path, *reads_to_keep: ModelScenarioRead) -> None:
    """Write the cached scenario-runs file the way the scenario stage leaves it; every other scenario read just now."""
    fresh = [read(d.id, run("success", 10), read_minutes_ago=0) for d in OTHERS]
    doc = ModelScenarioReads(
        updated_at=iso(NOW),
        reads=(*reads_to_keep, *fresh),
    )
    target = home / RT.scenario_reads_relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(doc.model_dump_json(indent=1), encoding="utf-8")


def _runner(answer: tuple[int, str, str]) -> FakeRunner:
    return FakeRunner({DECL.workflow: answer})


def test_r5_cached_run_list_a_timed_out_read_keeps_the_cached_run_list_while_it_is_young(
    tmp_path: Path,
) -> None:
    home = write_home(tmp_path)
    cached = read(DECL.id, run("success", 30), read_minutes_ago=10)
    _seed(home, cached)
    runner = _runner((124, "", "timed out after 25 s"))
    got = read_scenarios(RT, home, NOW, runner, clock=lambda: NOW)
    kept = next(r for r in got.reads if r.scenario == DECL.id)
    assert DECL.workflow in runner.read  # it was asked, and the ask failed
    assert kept.read == "read"
    assert [r.conclusion for r in kept.runs] == ["success"]
    assert (
        kept.read_at == cached.read_at
    )  # still the old read: it ages out, it is never refreshed by a failure


def test_r5_cached_run_list_a_failed_read_older_than_the_max_age_is_drawn_unread(
    tmp_path: Path,
) -> None:
    home = write_home(tmp_path)
    old_minutes = RT.scenario_reads_max_age_seconds // 60 + 5
    _seed(home, read(DECL.id, run("success", 90), read_minutes_ago=old_minutes))
    got = read_scenarios(
        RT, home, NOW, _runner((124, "", "timed out after 25 s")), clock=lambda: NOW
    )
    kept = next(r for r in got.reads if r.scenario == DECL.id)
    assert kept.read == "unread"
    assert "timed out" in kept.reason


def test_r5_cached_run_list_a_failed_read_with_nothing_cached_is_unread(
    tmp_path: Path,
) -> None:
    home = write_home(tmp_path)
    _seed(home)  # DECL has no cached read at all
    got = read_scenarios(
        RT, home, NOW, _runner((124, "", "timed out after 25 s")), clock=lambda: NOW
    )
    kept = next(r for r in got.reads if r.scenario == DECL.id)
    assert kept.read == "unread"


def test_r5_cached_run_list_a_workflow_known_missing_is_not_asked_about_again_inside_the_missing_refresh(
    tmp_path: Path,
) -> None:
    home = write_home(tmp_path)
    minutes = RT.scenario_refresh_seconds // 60 + 2  # older than a normal refresh
    assert minutes * 60 < RT.scenario_missing_refresh_seconds
    _seed(
        home,
        read(DECL.id, kind="no_workflow", read_minutes_ago=minutes, reason="HTTP 404"),
    )
    runner = _runner((124, "", "timed out after 25 s"))
    got = read_scenarios(RT, home, NOW, runner, clock=lambda: NOW)
    kept = next(r for r in got.reads if r.scenario == DECL.id)
    assert DECL.workflow not in runner.read
    assert kept.read == "no_workflow"


def test_r5_cached_run_list_control_a_missing_workflow_is_asked_again_once_the_missing_refresh_passed(
    tmp_path: Path,
) -> None:
    """The builder landing the workflow must show: past the missing refresh the reader asks again."""
    home = write_home(tmp_path)
    minutes = RT.scenario_missing_refresh_seconds // 60 + 2
    _seed(
        home,
        read(DECL.id, kind="no_workflow", read_minutes_ago=minutes, reason="HTTP 404"),
    )
    runner = _runner((0, runs_answer(("completed", "success", 5)), ""))
    got = read_scenarios(RT, home, NOW, runner, clock=lambda: NOW)
    kept = next(r for r in got.reads if r.scenario == DECL.id)
    assert DECL.workflow in runner.read
    assert kept.read == "read"
    assert kept.read_at == iso(NOW)  # the new read, not the cached 404


def test_r5_cached_run_list_an_unread_read_is_asked_again_on_the_next_run(
    tmp_path: Path,
) -> None:
    """A read that failed is not an answer: it is not held back for scenario_refresh_seconds like a read that worked."""
    home = write_home(tmp_path)
    _seed(
        home,
        read(
            DECL.id,
            kind="unread",
            read_minutes_ago=1,
            reason="exit 124: timed out after 25 s",
        ),
    )
    runner = _runner((0, runs_answer(("completed", "success", 5)), ""))
    got = read_scenarios(RT, home, NOW, runner, clock=lambda: NOW)
    kept = next(r for r in got.reads if r.scenario == DECL.id)
    assert DECL.workflow in runner.read
    assert kept.read == "read"


def test_r5_cached_run_list_control_a_read_that_worked_a_minute_ago_is_not_asked_again(
    tmp_path: Path,
) -> None:
    home = write_home(tmp_path)
    _seed(home, read(DECL.id, run("success", 30), read_minutes_ago=1))
    runner = _runner((0, runs_answer(("completed", "failure", 5)), ""))
    got = read_scenarios(RT, home, NOW, runner, clock=lambda: NOW)
    kept = next(r for r in got.reads if r.scenario == DECL.id)
    assert DECL.workflow not in runner.read
    assert [r.conclusion for r in kept.runs] == ["success"]


def test_r5_cached_run_list_each_read_records_how_long_it_took(tmp_path: Path) -> None:
    """The board says how long a read took, so a slow host shows as a number on the floor, not as a bare timeout."""
    home = write_home(tmp_path)
    _seed(home)
    got = read_scenarios(
        RT,
        home,
        NOW,
        _runner((0, runs_answer(("completed", "success", 5)), "")),
        clock=lambda: NOW,
    )
    fresh = next(r for r in got.reads if r.scenario == DECL.id)
    assert fresh.read == "read"
    assert fresh.read_seconds is not None
    assert fresh.read_seconds >= 0
    timed_out = read_scenarios(
        RT,
        write_home(tmp_path / "again"),
        NOW,
        _runner((124, "", "timed out after 25 s")),
        clock=lambda: NOW,
    )
    failed = next(r for r in timed_out.reads if r.scenario == DECL.id)
    assert failed.read == "unread"
    assert failed.read_seconds is not None
