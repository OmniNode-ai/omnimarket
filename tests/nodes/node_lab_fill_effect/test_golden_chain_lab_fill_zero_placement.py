# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: a tick that leaves free slots unplaced says so and acts (OMN-17427).

The numbers are the 2026-10-17 night: 19 of 25 lab-fill ticks placed 0 lanes with up to
10 free slots (09:56Z: 10 free, 3 eligible, 0 placed); OMN-19364 was refused by the
brief check on every tick from 04:38Z; the 09:56Z row listed a lane
``skipped:brief-check-refused`` while its ``refusals=`` cell read ``none``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from omnimarket.nodes.node_lab_fill_effect.handlers import (
    HandlerLabFillLaunch,
    HandlerLabFillTick,
)
from omnimarket.nodes.node_lab_fill_effect.models import (
    ModelLabFillTickHost,
    ModelLabFillTickRequest,
    ModelLabFillTickResult,
)
from omnimarket.nodes.node_lab_fill_effect.protocols import (
    LabFillClaimFacts,
    LabFillCommandOutcome,
    LabFillPortError,
)

from .fakes import (
    FakeApproved,
    FakeBlocks,
    FakeChecks,
    FakeClock,
    FakeOwners,
    FakeRunner,
)

BRIEF_REFUSED = LabFillCommandOutcome(
    2,
    "REMOTE-LANE-MINT-REFUSED a lab lane cannot mint a ticket\n",
    "",
)
DETACHED = LabFillCommandOutcome(
    0, "DETACHED lane=x receipt=/state/x/receipt.json\n", ""
)


def _row(n: int, kind: str = "wiring", repo: str = "omnimarket") -> dict[str, object]:
    return {
        "id": f"row-{n}",
        "kind": kind,
        "ticket": f"OMN-{n}",
        "repo": repo,
        "goal": f"goal {n}",
        "acceptance_check": f"check {n}",
    }


@dataclass
class Memory:
    """The tick's memory file: one reader and one writer over a dict."""

    files: dict[str, Mapping[str, object]] = field(default_factory=dict)
    fail_write: bool = False

    def read(self, path: str) -> Mapping[str, object] | None:
        return self.files.get(path)

    def write(self, path: str, value: Mapping[str, object]) -> None:
        if self.fail_write:
            raise LabFillPortError("disk full")
        self.files[path] = value


@dataclass
class ScriptedRunner(FakeRunner):
    """Refuses the lanes whose ticket is in ``refuse``; detaches the rest."""

    refuse: frozenset[str] = frozenset()

    def run(
        self, args: Any, *, env: Mapping[str, str], timeout_s: float
    ) -> LabFillCommandOutcome:
        self.calls.append((list(args), dict(env), timeout_s))
        ticket = args[args.index("--ticket") + 1]
        return BRIEF_REFUSED if ticket in self.refuse else DETACHED


def _request(tmp_path: Path, **kw: Any) -> ModelLabFillTickRequest:
    base: dict[str, Any] = {
        "run_key": "2026-10-17T0956Z",
        "fire_id": "20261017T095600Z-lab-fill",
        "hosts": (ModelLabFillTickHost(host="h201", free=10),),
        "ledger_path": "/l",
        "approved_work_path": "/a",
        "brief_dir": str(tmp_path / "briefs"),
        "state_path": "/state/lab-fill-tick-memory.json",
        "operator_id": "op",
        "parent": "orchestrator-bb1a",
        "parent_ticket": "OMN-17427",
        "authority_ruling": "2026-10-17T09:03:06Z",
        "authority_lane": "orchestrator-bb1a",
        "mvp_repos": ("omnimarket",),
    }
    base.update(kw)
    return ModelLabFillTickRequest(**base)


def _handler(
    rows: list[dict[str, object]],
    runner: FakeRunner,
    memory: Memory,
    clock: FakeClock,
) -> HandlerLabFillTick:
    approved = FakeApproved(rows={str(r["id"]): r for r in rows}, all_rows=rows)
    launch = HandlerLabFillLaunch(
        checks=FakeChecks(),
        approved=approved,
        blocks=FakeBlocks(),
        runner=runner,
        clock=clock,
    )
    return HandlerLabFillTick(
        approved=approved,
        owners=FakeOwners(
            claims_facts=LabFillClaimFacts(index={}, open_claims=[], staleness_hours=12)
        ),
        launch=launch,
        clock=clock,
        memory_reader=memory,
        memory_writer=memory,
    )


def _tick(
    handler: HandlerLabFillTick, clock: FakeClock, tmp_path: Path, **kw: Any
) -> ModelLabFillTickResult:
    clock.t += 1200  # the 20-minute fire interval
    return handler.handle(_request(tmp_path, **kw))


def _diagnose_calls(runner: FakeRunner) -> list[list[str]]:
    return [c[0] for c in runner.calls if "lab-fill-diagnose" in " ".join(c[0])]


# -- refusals= counts every lane not placed ---------------------------------------------


def test_every_unplaced_lane_is_a_refusal_with_its_reason(tmp_path: Path) -> None:
    # 09:56Z: 10 free slots, 3 eligible candidates, 0 placed
    rows = [_row(19364), _row(18349), _row(20489)]
    runner = ScriptedRunner(refuse=frozenset({"OMN-19364", "OMN-18349", "OMN-20489"}))
    clock = FakeClock()
    result = _handler(rows, runner, Memory(), clock).handle(_request(tmp_path))
    assert result.free_slots == 10
    assert result.placed == 0
    assert [(r.ticket, r.reason) for r in result.refusals] == [
        ("OMN-19364", "brief-check-refused"),
        ("OMN-18349", "brief-check-refused"),
        ("OMN-20489", "brief-check-refused"),
    ]
    assert all("REMOTE-LANE-MINT-REFUSED" in r.detail for r in result.refusals)
    assert result.refusals_cell != "none"
    for r in result.refusals:
        assert f"{r.lane}@h201:brief-check-refused" in result.refusals_cell


def test_a_placed_lane_is_not_a_refusal_and_no_refusals_reads_none(
    tmp_path: Path,
) -> None:
    rows = [_row(1), _row(2)]
    runner = ScriptedRunner(refuse=frozenset({"OMN-1"}))
    result = _handler(rows, runner, Memory(), FakeClock()).handle(_request(tmp_path))
    assert result.placed == 1
    assert [(r.ticket, r.reason) for r in result.refusals] == [
        ("OMN-1", "brief-check-refused")
    ]
    clean = _handler([_row(3)], ScriptedRunner(), Memory(), FakeClock()).handle(
        _request(tmp_path)
    )
    assert clean.refusals == ()
    assert clean.refusals_cell == "none"


def test_a_stopped_tick_counts_the_lanes_it_never_tried(tmp_path: Path) -> None:
    class Down(FakeRunner):
        def run(self, args: Any, *, env: Mapping[str, str], timeout_s: float) -> Any:
            raise LabFillPortError("runner script missing")

    result = _handler(
        [_row(1), _row(2), _row(3)], Down(), Memory(), FakeClock()
    ).handle(_request(tmp_path))
    assert result.stopped == "runner-unavailable"
    assert [(r.ticket, r.reason) for r in result.refusals] == [
        ("OMN-1", "runner-unavailable"),
        ("OMN-2", "untried:runner-unavailable"),
        ("OMN-3", "untried:runner-unavailable"),
    ]


def test_a_plan_only_tick_reports_no_refusals_and_keeps_no_memory(
    tmp_path: Path,
) -> None:
    memory = Memory()
    result = _handler([_row(1)], ScriptedRunner(), memory, FakeClock()).handle(
        _request(tmp_path, apply=False)
    )
    assert result.refusals == ()
    assert result.alarm is None
    assert memory.files == {}


# -- a candidate refused by the brief check twice is parked ------------------------------


def test_a_candidate_refused_by_the_brief_check_twice_is_parked(tmp_path: Path) -> None:
    rows = [_row(19364), _row(18349)]
    runner = ScriptedRunner(refuse=frozenset({"OMN-19364"}))
    clock = FakeClock()
    memory = Memory()
    handler = _handler(rows, runner, memory, clock)

    first = _tick(handler, clock, tmp_path)
    assert [r.ticket for r in first.refusals] == ["OMN-19364"]
    assert [s.reason for s in first.skipped if s.ticket == "OMN-19364"] == [
        "brief-check-refused"
    ]

    second = _tick(handler, clock, tmp_path)
    assert [r.ticket for r in second.refusals] == ["OMN-19364"]
    parked = [s for s in second.skipped if s.reason.startswith("parked:")]
    assert parked == []  # parked by this tick's refusal, not yet skipped for it
    assert second.parked == ("OMN-19364",)

    calls_before = len(runner.calls)
    third = _tick(handler, clock, tmp_path)
    assert third.refusals == ()
    assert third.refusals_cell == "none"
    assert [(s.ticket, s.reason) for s in third.skipped] == [
        ("OMN-19364", "parked:brief-check-refused")
    ]
    assert "REMOTE-LANE-MINT-REFUSED" in third.skipped[0].detail
    # the parked candidate cost no launch; the other row still went
    assert [d.ticket for d in third.dispatched] == ["OMN-18349"]
    assert len(runner.calls) == calls_before + 1


def test_a_parked_candidate_is_retried_after_the_park_expires(tmp_path: Path) -> None:
    runner = ScriptedRunner(refuse=frozenset({"OMN-19364"}))
    clock = FakeClock()
    handler = _handler([_row(19364)], runner, Memory(), clock)
    _tick(handler, clock, tmp_path)
    _tick(handler, clock, tmp_path)
    skipped = _tick(handler, clock, tmp_path)
    assert [s.reason for s in skipped.skipped] == ["parked:brief-check-refused"]
    clock.t += 24 * 3600
    retried = _tick(handler, clock, tmp_path)
    assert [r.ticket for r in retried.refusals] == ["OMN-19364"]
    assert retried.skipped[0].reason == "brief-check-refused"


def test_a_refusal_for_another_reason_does_not_park(tmp_path: Path) -> None:
    runner = FakeRunner(outcome=LabFillCommandOutcome(1, "", "host busy"))
    clock = FakeClock()
    handler = _handler([_row(1)], runner, Memory(), clock)
    for _ in range(3):
        result = _tick(handler, clock, tmp_path)
        assert [r.reason for r in result.refusals] == ["runner-refused"]
        assert result.parked == ()
    # three refused tries at the row, each tick; the diagnose lane's own attempts are apart
    assert len(runner.calls) - len(_diagnose_calls(runner)) == 3


def test_a_placement_clears_the_refusal_count(tmp_path: Path) -> None:
    class Flaky(ScriptedRunner):
        def run(self, args: Any, *, env: Mapping[str, str], timeout_s: float) -> Any:
            self.refuse = (
                frozenset({"OMN-1"}) if len(self.calls) % 2 == 0 else frozenset()
            )
            return super().run(args, env=env, timeout_s=timeout_s)

    clock = FakeClock()
    handler = _handler([_row(1)], Flaky(), Memory(), clock)
    outcomes = [_tick(handler, clock, tmp_path) for _ in range(4)]
    # refused, placed, refused, placed: never two refusals in a row, so never parked
    assert [o.placed for o in outcomes] == [0, 1, 0, 1]
    assert all(o.parked == () for o in outcomes)


# -- free slots and zero placed for two ticks: one diagnose lane, an event -------------


def test_two_idle_ticks_dispatch_one_diagnose_lane_and_say_so(tmp_path: Path) -> None:
    runner = ScriptedRunner(refuse=frozenset({"OMN-19364", "OMN-18349", "OMN-20489"}))
    clock = FakeClock()
    handler = _handler([_row(19364), _row(18349), _row(20489)], runner, Memory(), clock)

    first = _tick(handler, clock, tmp_path)
    assert first.placed == 0
    assert first.alarm is not None
    assert (first.alarm.streak, first.alarm.diagnose) == (1, "none")
    assert _diagnose_calls(runner) == []

    second = _tick(handler, clock, tmp_path)
    assert second.alarm is not None
    assert second.alarm.streak == 2
    assert second.alarm.free_slots == 10
    assert second.alarm.diagnose == "dispatched"
    calls = _diagnose_calls(runner)
    assert len(calls) == 1
    args = calls[0]
    assert args[args.index("--ticket") + 1] == "OMN-17427"
    assert args[args.index("--lane") + 1] == second.alarm.diagnose_lane
    assert second.alarm.diagnose_lane.startswith("lab-fill-diagnose-")
    assert second.alarm.diagnose_receipt == "/state/x/receipt.json"
    brief = (tmp_path / "briefs" / f"{second.alarm.diagnose_lane}.md").read_text()
    assert "free_slots=10" in brief
    assert "OMN-19364" in brief
    assert "brief-check-refused" in brief
    assert "never merge or arm" in brief
    # the diagnose lane is not a placement of approved work
    assert second.placed == 0


def test_the_diagnose_lane_is_deduplicated(tmp_path: Path) -> None:
    runner = ScriptedRunner(refuse=frozenset({"OMN-1"}))
    clock = FakeClock()
    handler = _handler([_row(1)], runner, Memory(), clock)
    results = [_tick(handler, clock, tmp_path) for _ in range(5)]
    assert [r.alarm.diagnose for r in results if r.alarm] == [
        "none",
        "dispatched",
        "deduplicated",
        "deduplicated",
        "deduplicated",
    ]
    assert len(_diagnose_calls(runner)) == 1
    # a long run of idle ticks is dispatched again once the cooldown has passed
    clock.t += 4 * 3600
    again = _tick(handler, clock, tmp_path)
    assert again.alarm is not None
    assert again.alarm.diagnose == "dispatched"
    assert len(_diagnose_calls(runner)) == 2


def test_a_placed_lane_or_a_full_pool_resets_the_streak(tmp_path: Path) -> None:
    clock = FakeClock()
    runner = ScriptedRunner(refuse=frozenset({"OMN-1"}))
    handler = _handler([_row(1)], runner, Memory(), clock)
    idle = _tick(handler, clock, tmp_path)
    assert idle.alarm is not None
    assert idle.alarm.streak == 1
    full = _tick(
        handler, clock, tmp_path, hosts=(ModelLabFillTickHost(host="h201", free=0),)
    )
    assert full.outcome == "no-headroom"
    assert full.alarm is not None
    assert full.alarm.streak == 0
    after = _tick(handler, clock, tmp_path)
    assert after.alarm is not None
    assert after.alarm.streak == 1
    assert _diagnose_calls(runner) == []


def test_a_refused_diagnose_lane_is_reported_and_tried_again_next_tick(
    tmp_path: Path,
) -> None:
    runner = ScriptedRunner(refuse=frozenset({"OMN-1", "OMN-17427"}))
    clock = FakeClock()
    handler = _handler([_row(1)], runner, Memory(), clock)
    _tick(handler, clock, tmp_path)
    second = _tick(handler, clock, tmp_path)
    assert second.alarm is not None
    assert second.alarm.diagnose == "refused"
    assert second.alarm.diagnose_detail.startswith("brief-check-refused")
    third = _tick(handler, clock, tmp_path)
    assert third.alarm is not None
    assert third.alarm.diagnose == "refused"
    assert len(_diagnose_calls(runner)) == 2


def test_the_idle_streak_survives_between_ticks_in_the_memory_file(
    tmp_path: Path,
) -> None:
    memory = Memory()
    runner = ScriptedRunner(refuse=frozenset({"OMN-1"}))
    clock = FakeClock()
    _tick(_handler([_row(1)], runner, memory, clock), clock, tmp_path)
    assert memory.files["/state/lab-fill-tick-memory.json"]["idle_streak"] == 1
    # a fresh handler (a new fire) reads the file and finishes the second idle tick
    second = _tick(_handler([_row(1)], runner, memory, clock), clock, tmp_path)
    assert second.alarm is not None
    assert second.alarm.diagnose == "dispatched"


def test_no_memory_path_means_no_alarm_and_the_tick_says_so(tmp_path: Path) -> None:
    result = _handler(
        [_row(1)], ScriptedRunner(refuse=frozenset({"OMN-1"})), Memory(), FakeClock()
    ).handle(_request(tmp_path, state_path=""))
    assert result.alarm is None
    assert "memory off" in result.detail
    assert [r.ticket for r in result.refusals] == ["OMN-1"]


def test_an_unwritable_memory_file_is_named_not_swallowed(tmp_path: Path) -> None:
    memory = Memory(fail_write=True)
    result = _handler(
        [_row(1)], ScriptedRunner(refuse=frozenset({"OMN-1"})), memory, FakeClock()
    ).handle(_request(tmp_path))
    assert "memory-unwritable" in result.detail
    assert "disk full" in result.detail
