# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Machine bays, live lanes and the cloud bay (OMN-21733): what each field is read from, and what is drawn unread."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from omnimarket.models.model_factory_floor_input import (
    ModelFloorCloudAccount,
    ModelFloorHostRow,
    ModelFloorLaneProcess,
    ModelFloorLinearIssue,
    ModelFloorMachineDecl,
    ModelFloorPlacedHost,
    ModelFloorPoolReading,
    ModelFloorReceipt,
    ModelFloorRunner,
    ModelFloorRunnerFleet,
)
from omnimarket.nodes.node_factory_floor_compute.handlers.handler_factory_floor import (
    compute_factory_floor,
)
from tests.nodes.node_factory_floor_compute.support import (
    NOW,
    ago,
    config,
    pr,
    request,
    row,
    task,
    wave_plan,
)

pytestmark = pytest.mark.unit

MACHINES = (
    ModelFloorMachineDecl(host="h051", label="h051", runner_labels=("label-one",)),
    ModelFloorMachineDecl(host="h202", label="h202"),
    ModelFloorMachineDecl(host="h105", label="h105"),
    ModelFloorMachineDecl(host="h200", label="h200"),
    ModelFloorMachineDecl(host="h101", label="h101"),
)


def reading(host: str, **cells: Any) -> ModelFloorPoolReading:
    base: dict[str, Any] = {
        "host": host,
        "read_at": ago(2),
        "cores": 80,
        "load1": 20.0,
        "mem_free_gb": 300.0,
        "mem_total_gb": 377.0,
        "swap_used_pct": 5.0,
        "cap": 24,
        "slots": 20,
    }
    return ModelFloorPoolReading(**{**base, **cells})


def receipt(lane: str, **cells: Any) -> ModelFloorReceipt:
    base: dict[str, Any] = {
        "lane": lane,
        "run_id": f"rlane-{lane}-1",
        "host": "h051",
        "status": "running",
        "started_at": ago(30),
        "model": "sonnet",
    }
    return ModelFloorReceipt(**{**base, **cells})


def lane_rows(lane: str, *, claim_ago: float = 60, **claim: Any) -> tuple[Any, ...]:
    cells: dict[str, Any] = {
        "tickets": ("OMN-5",),
        "actor": "claude:sonnet:workflow-lane",
        "model": "claude-sonnet",
        "text": "Fix the reds on omnimarket#4679. Then more words.",
        **claim,
    }
    return (row("CLAIM", claim_ago, lane, **cells),)


def floor(**extra: Any) -> Any:
    return compute_factory_floor(request(machines=MACHINES, **extra)).floor


def test_a_machine_with_a_fresh_reading_is_drawn_against_its_cap_load_and_memory() -> (
    None
):
    result = floor(
        pool=(reading("h051"),),
        host_table=(ModelFloorHostRow(host="h051", cap=24),),
        placed=(ModelFloorPlacedHost(host="h051", units=5, lanes=2),),
        ledger=lane_rows("lane-a", host="h051"),
        receipts=(receipt("lane-a"),),
    )
    bay = next(m for m in result.machines if m.host == "h051")
    assert (bay.state, bay.cap, bay.lanes, bay.lanes_placed) == (
        "read",
        24,
        ("lane-a",),
        1,
    )
    assert (bay.load_per_core, bay.mem_free_gb, bay.swap_used_pct) == (0.25, 300.0, 5.0)
    assert (bay.units_placed, bay.unit_work, bay.read_age_min, bay.stale) == (
        5,
        3,
        2,
        False,
    )


def test_the_host_table_cap_wins_over_the_reading_cap() -> None:
    result = floor(
        pool=(reading("h051", cap=99),),
        host_table=(ModelFloorHostRow(host="h051", cap=24),),
    )
    assert result.machines[0].cap == 24


def test_a_host_the_pool_read_could_not_read_is_unread_with_nothing_drawn() -> None:
    result = floor(
        pool=(
            ModelFloorPoolReading(
                host="h105", read_at=ago(1), error="rc=255 timed out after 20s"
            ),
        )
    )
    bay = next(m for m in result.machines if m.host == "h105")
    assert (bay.state, bay.load1, bay.mem_free_gb, bay.cap) == (
        "unread",
        None,
        None,
        None,
    )
    assert "timed out" in bay.note
    absent = next(m for m in result.machines if m.host == "h051")
    assert absent.state == "unread"
    assert absent.note == "the pool read held no row for this machine"


def test_no_pool_read_at_all_leaves_every_bay_unread_not_empty() -> None:
    result = floor()
    assert {m.state for m in result.machines} == {"unread"}
    assert result.machines[0].note == "no pool read has been recorded"
    assert (result.machines[0].units_placed, result.machines[0].unit_work) == (
        None,
        None,
    )


def test_a_host_out_of_selection_is_in_repair_with_its_reason_cut_at_the_first_clause() -> (
    None
):
    result = floor(
        pool=(reading("h051"),),
        host_table=(
            ModelFloorHostRow(
                host="h202",
                available=False,
                unavailable_reason="down for repair 2026-10-23; back when a file exists",
            ),
        ),
    )
    bay = next(m for m in result.machines if m.host == "h202")
    assert (bay.state, bay.note) == ("repair", "down for repair 2026-10-23")


def test_a_reading_older_than_the_stale_bar_is_marked_stale() -> None:
    result = floor(pool=(reading("h051", read_at=NOW - timedelta(minutes=90)),))
    assert result.machines[0].stale is True
    assert result.machines[0].read_age_min == 90


def test_a_machine_the_overlay_does_not_declare_is_never_drawn() -> None:
    result = floor(pool=(reading("h051"), reading("h203")))
    assert [m.host for m in result.machines] == [m.host for m in MACHINES]
    lanes = floor(
        pool=(reading("h203"),), ledger=lane_rows("lane-x", host="h203")
    ).lanes
    assert (lanes[0].placement, lanes[0].host) == ("unplaced", "")


def test_ci_runners_are_counted_by_label_and_unread_without_an_observation_or_label() -> (
    None
):
    fleet = ModelFloorRunnerFleet(
        observed_at=ago(3),
        runners=(
            ModelFloorRunner(host_label="label-one", status="busy"),
            ModelFloorRunner(host_label="label-one", status="online"),
            ModelFloorRunner(host_label="label-one", status="offline"),
            ModelFloorRunner(host_label="label-other", status="busy"),
        ),
    )
    machines = floor(runner_fleet=fleet).machines
    first = machines[0].runners
    assert (first.state, first.total, first.online, first.busy) == ("read", 3, 2, 1)
    assert machines[1].runners.state == "unread"
    assert "no runner label" in machines[1].runners.note
    assert (
        floor().machines[0].runners.note
        == "no runner fleet observation has been recorded"
    )


def test_a_live_lane_carries_title_kind_status_pr_ci_run_time_and_station() -> None:
    ledger = (
        *lane_rows("lane-a", host="h051", prs=("OmniNode-ai/omnimarket#4702",)),
        row(
            "STATUS",
            4,
            "lane-a",
            text="pushed the effect fields; waiting on CI " + "x" * 200,
        ),
    )
    watched = pr("omnimarket#4702", title="feat: lanes", tickets=("OMN-5",)).model_copy(
        update={"ci": "red", "ci_note": "2 red of 150"}
    )
    result = compute_factory_floor(
        request(
            machines=MACHINES,
            ledger=ledger,
            linear=(
                ModelFloorLinearIssue(id="OMN-5", title="The floor shows its lanes"),
            ),
            receipts=(receipt("lane-a", started_at=ago(50)),),
            prs=(watched,),
        )
    ).floor
    lane = result.lanes[0]
    assert (lane.ticket, lane.title, lane.title_source) == (
        "OMN-5",
        "The floor shows its lanes",
        "linear",
    )
    assert (lane.kind, lane.engine, lane.placement, lane.host) == (
        "fix",
        "claude",
        "machine",
        "h051",
    )
    assert (lane.status_source, lane.status_age_min) == ("status", 4)
    assert len(lane.status_text) <= 140
    assert lane.status_full.startswith("pushed the effect")
    assert (lane.pr, lane.pr_state, lane.pr_ci, lane.pr_ci_note) == (
        "omnimarket#4702",
        "OPEN",
        "red",
        "2 red of 150",
    )
    assert (lane.run_minutes, lane.stalled, lane.silent_minutes) == (50, False, 4)


def test_a_lane_silent_for_45_minutes_is_stalled_and_one_at_44_is_not() -> None:
    quiet = floor(
        ledger=(
            *lane_rows("quiet", claim_ago=120),
            row("STATUS", 45, "quiet", text="x"),
        )
    )
    busy = floor(
        ledger=(*lane_rows("busy", claim_ago=120), row("STATUS", 44, "busy", text="x"))
    )
    assert quiet.lanes[0].stalled is True
    assert busy.lanes[0].stalled is False


def test_a_lane_without_a_status_row_shows_its_claim_as_what_it_does() -> None:
    lane = floor(ledger=lane_rows("lane-a")).lanes[0]
    assert (lane.status_source, lane.title_source, lane.pr, lane.pr_ci) == (
        "claim",
        "claim",
        "",
        "",
    )
    assert lane.title == "Fix the reds on omnimarket#4679"


def test_a_pr_the_watcher_does_not_hold_is_drawn_unread() -> None:
    lane = floor(ledger=lane_rows("lane-a", prs=("omnimarket#9",))).lanes[0]
    assert (lane.pr, lane.pr_state, lane.pr_ci) == ("omnimarket#9", "unread", "unread")


def test_a_closed_lane_and_a_lane_silent_past_the_live_window_are_not_listed() -> None:
    ledger = (
        *lane_rows("done", claim_ago=100),
        row("TERMINAL", 10, "done", outcome="done"),
        *lane_rows("dead", claim_ago=300),
        *lane_rows("alive", claim_ago=100),
    )
    assert [lane.lane for lane in floor(ledger=ledger).lanes] == ["alive"]


def test_a_lane_that_claimed_again_after_its_terminal_is_live() -> None:
    ledger = (
        row("TERMINAL", 200, "again", outcome="done"),
        *lane_rows("again", claim_ago=20),
    )
    assert [lane.lane for lane in floor(ledger=ledger).lanes] == ["again"]


@pytest.mark.parametrize(
    "cells",
    [
        {"final": True, "status": "landing-failed", "ended_at": ago(5)},
        {"status": "landing-failed"},
        {"ended_at": ago(5)},
    ],
)
def test_finished_receipt_closes_cx_worker_without_a_terminal(
    cells: dict[str, Any],
) -> None:
    result = floor(
        ledger=lane_rows("cx-5614-0410", host="h051"),
        receipts=(receipt("cx-5614-0410", **cells),),
        pool=(reading("h051"),),
    )
    assert result.lanes == ()
    assert result.machines[0].lanes_placed == 0
    assert result.bays[0].running == 0
    assert result.inactive_lanes[0].liveness == "finished"


def test_a_finished_receipt_from_an_older_run_does_not_close_a_reclaimed_lane() -> None:
    result = floor(
        ledger=lane_rows("cx-5614-0410", claim_ago=10, host="h051"),
        receipts=(receipt("cx-5614-0410", final=True, ended_at=ago(20)),),
    )
    assert [lane.lane for lane in result.lanes] == ["cx-5614-0410"]


@pytest.mark.parametrize(
    "lane", ["landing-L25183", "landing-L25192", "landing-L25196", "pfix-2062-r-1744"]
)
def test_a_dead_runner_with_a_running_receipt_occupies_no_slot(lane: str) -> None:
    result = floor(
        ledger=lane_rows(lane, host="h051"),
        receipts=(receipt(lane, runner_alive=False),),
        pool=(reading("h051"),),
    )
    assert result.lanes == ()
    assert result.machines[0].lanes_placed == result.bays[0].running == 0
    assert [(item.lane, item.liveness) for item in result.inactive_lanes] == [
        (lane, "dead")
    ]


@pytest.mark.parametrize("alive", [True, None])
def test_a_live_or_unread_runner_stays_quiet(alive: bool | None) -> None:
    result = floor(
        ledger=lane_rows("quiet-live", host="h051"),
        receipts=(receipt("quiet-live", runner_alive=alive),),
    )
    assert result.lanes[0].stalled is True
    assert result.lanes[0].liveness == ("alive" if alive else "unread")
    assert result.inactive_lanes == ()
    assert result.machines[0].lanes_placed == 1


@pytest.mark.parametrize("lane", ["landing-cause-L25919", "landing-cause-L26040"])
def test_a_hostless_cause_claim_with_no_process_is_dead(lane: str) -> None:
    result = floor(
        ledger=lane_rows(lane, claim_ago=148),
        lane_processes=(
            ModelFloorLaneProcess(
                lane=lane,
                started_at=ago(148),
                observed_at=NOW,
                alive=False,
                detail="controller worker group and tag are absent",
            ),
        ),
    )
    assert result.lanes == ()
    assert result.inactive_lanes[0].liveness == "dead"


def test_stale_process_evidence_and_a_receipt_for_another_run_cannot_close_a_claim() -> (
    None
):
    result = floor(
        ledger=lane_rows("again", run_id="current"),
        receipts=(receipt("again", run_id="old", runner_alive=False, final=True),),
        lane_processes=(
            ModelFloorLaneProcess(
                lane="again",
                started_at=ago(60),
                observed_at=ago(90),
                alive=False,
                detail="old process read",
            ),
        ),
    )
    assert [item.lane for item in result.lanes] == ["again"]
    assert result.lanes[0].liveness == "unread"


def test_kind_comes_from_the_declared_rules_in_order() -> None:
    def kind_of(text: str, actor: str = "x") -> str:
        rows = (row("CLAIM", 10, "k", tickets=("OMN-5",), actor=actor, text=text),)
        return str(floor(ledger=rows).lanes[0].kind)

    assert kind_of("anything", "script:landing-controller") == "landing worker"
    assert kind_of("Find the real cause of cloud session push failure") == "diagnose"
    assert kind_of("Burn-down of the docs backlog") == "burn-down"
    assert kind_of("Write a plan for the runner fleet") == "plan"
    assert kind_of("Build the page") == "build"


def test_engine_is_codex_when_the_model_names_it_and_blank_when_unknown() -> None:
    assert floor(ledger=lane_rows("c", model="codex_high")).lanes[0].engine == "codex"
    plain = (row("CLAIM", 5, "p", tickets=("OMN-5",), text="t"),)
    assert floor(ledger=plain).lanes[0].engine == ""


def test_a_lane_links_to_the_station_that_holds_its_ticket() -> None:
    plan = wave_plan()
    manifest = plan.manifest
    assert manifest is not None
    held = manifest.model_copy(
        update={"tasks": (*manifest.tasks[:1], task("W0.2", "0", tickets=("OMN-5",)))}
    )
    plan = plan.model_copy(update={"manifest": held})
    lane = compute_factory_floor(
        request(plans=(plan,), machines=MACHINES, ledger=lane_rows("lane-a"))
    ).floor.lanes[0]
    assert lane.station is not None
    # Every task is its own station, so the lane links to the task's station.
    assert (lane.station.station_id, lane.station.item_id) == ("W0.2", "W0.2")
    assert floor(ledger=lane_rows("lane-a")).lanes[0].station is None


def cloud_receipt(lane: str, minutes_ago: float, **cells: Any) -> ModelFloorReceipt:
    base: dict[str, Any] = {
        "host": "cloud:h200",
        "placement": "cloud",
        "account": "h200",
        "started_at": ago(minutes_ago),
    }
    return receipt(lane, **{**base, **cells})


def test_the_cloud_bay_lists_ready_accounts_sessions_and_a_lamp_when_the_newest_could_not_push() -> (
    None
):
    result = floor(
        cloud_accounts=(
            ModelFloorCloudAccount(
                name="h200",
                login="ok",
                cap=14,
                active=("run-1",),
                credit_remaining=244.0,
            ),
            ModelFloorCloudAccount(name="h101", login="no-claude-ai-login"),
        ),
        receipts=(
            cloud_receipt(
                "old",
                300,
                final=True,
                status="cloud-no-pr",
                ended_at=ago(200),
                session_detail="push refused (403)",
                session_needs="authorize the repository",
            ),
            cloud_receipt("live", 20, status="running"),
        ),
    ).cloud
    assert [
        (a.name, a.cap, a.open_sessions, a.credit_remaining) for a in result.accounts
    ] == [("h200", 14, 1, 244.0)]
    assert result.note == "1 listed machines hold no cloud login"
    assert [(s.lane, s.open) for s in result.sessions] == [
        ("live", True),
        ("old", False),
    ]
    assert len(result.lamps) == 1
    assert "push refused (403)" in result.lamps[0].evidence
    assert "authorize" in result.lamps[0].evidence


def test_a_newer_finished_cloud_session_that_pushed_clears_the_lamp() -> None:
    result = floor(
        cloud_accounts=(ModelFloorCloudAccount(name="h200", login="ok"),),
        receipts=(
            cloud_receipt(
                "old", 300, final=True, status="cloud-no-pr", ended_at=ago(200)
            ),
            cloud_receipt("new", 100, final=True, status="done", ended_at=ago(50)),
        ),
    ).cloud
    assert result.lamps == ()


def test_an_unreadable_cloud_accounts_file_leaves_the_cloud_bay_unread() -> None:
    result = floor().cloud
    assert (result.state, result.accounts) == ("unread", ())


def test_a_cloud_lane_is_placed_in_the_cloud_and_carries_its_account() -> None:
    lane = floor(
        ledger=lane_rows("lane-a"), receipts=(cloud_receipt("lane-a", 20),)
    ).lanes[0]
    assert (lane.placement, lane.host, lane.cloud_account) == ("cloud", "", "h200")


def test_sources_say_which_inputs_were_read() -> None:
    sources = compute_factory_floor(
        request(machines=MACHINES, pool=(reading("h051"),), receipts=())
    ).sources
    assert (
        sources.receipts_read,
        sources.cloud_accounts_read,
        sources.runner_fleet_read,
    ) == (True, False, False)
    assert sources.pool_read_at != ""


def test_a_cloud_account_or_session_of_a_machine_the_overlay_does_not_declare_is_never_drawn() -> (
    None
):
    result = floor(
        cloud_accounts=(
            ModelFloorCloudAccount(name="h200", login="ok", cap=14),
            ModelFloorCloudAccount(name="h203", login="ok", cap=10),
        ),
        receipts=(
            cloud_receipt("mine", 10),
            cloud_receipt("theirs", 5, account="h203"),
        ),
    ).cloud
    assert [a.name for a in result.accounts] == ["h200"]
    assert [s.lane for s in result.sessions] == ["mine"]


def test_a_lane_takes_its_machine_from_its_newest_row_that_names_one() -> None:
    ledger = (
        *lane_rows("moved"),
        row("STATUS", 20, "moved", text="started", host="h101"),
        row("STATUS", 5, "moved", text="restarted", host="h051"),
        row("STATUS", 2, "moved", text="no host on this row"),
    )
    lane = floor(ledger=ledger).lanes[0]
    assert (lane.placement, lane.host) == ("machine", "h051")


def test_the_section_states_the_stall_and_window_it_judged_lanes_by() -> None:
    result = floor()
    assert (result.stall_minutes, result.lane_window_minutes) == (45, 180)


def test_a_host_the_table_takes_out_of_selection_for_another_reason_is_out_not_in_repair() -> (
    None
):
    result = floor(
        host_table=(
            ModelFloorHostRow(
                host="h051",
                available=False,
                unavailable_reason="placement paused for an install",
            ),
        )
    )
    bay = result.machines[0]
    assert (bay.state, bay.note) == ("out", "placement paused for an install")


def test_an_account_draws_the_open_sessions_of_its_own_list_not_the_entries_of_the_accounts_file() -> (
    None
):
    result = floor(
        cloud_accounts=(
            ModelFloorCloudAccount(
                name="h200",
                login="ok",
                cap=17,
                active=tuple(f"stale-{n}" for n in range(7)),
            ),
        ),
        receipts=(
            cloud_receipt("live-a", 10, run_id="r-a"),
            cloud_receipt("live-b", 20, run_id="r-b"),
            cloud_receipt("live-c", 30, run_id="r-c"),
            cloud_receipt("done", 90, run_id="r-d", final=True, status="done"),
        ),
    ).cloud
    (account,) = result.accounts
    assert account.open_sessions == 3
    assert account.open_sessions == sum(
        1 for s in result.sessions if s.account == "h200" and s.open
    )
    assert "7" in account.record_note
    assert "3" in account.record_note


def test_an_account_whose_file_entries_agree_with_its_list_carries_no_stale_note() -> (
    None
):
    result = floor(
        cloud_accounts=(
            ModelFloorCloudAccount(name="h200", login="ok", active=("r-a",)),
        ),
        receipts=(cloud_receipt("live-a", 10, run_id="r-a"),),
    ).cloud
    assert (result.accounts[0].open_sessions, result.accounts[0].record_note) == (
        1,
        "",
    )


def test_the_open_count_covers_an_open_session_the_capped_list_would_cut() -> None:
    limit = config().cloud_sessions_limit
    finished = tuple(
        cloud_receipt(f"done-{n}", 10 + n, run_id=f"d{n}", final=True, status="done")
        for n in range(limit + 2)
    )
    result = floor(
        cloud_accounts=(ModelFloorCloudAccount(name="h200", login="ok"),),
        receipts=(*finished, cloud_receipt("old-live", 600, run_id="old")),
    ).cloud
    assert result.accounts[0].open_sessions == 1
    assert [s.lane for s in result.sessions if s.open] == ["old-live"]
    assert len(result.sessions) <= limit


def test_every_ready_account_is_drawn_and_an_unread_cap_or_count_stays_unread() -> None:
    result = floor(
        cloud_accounts=(
            ModelFloorCloudAccount(name="h200", login="ok", cap=17),
            ModelFloorCloudAccount(name="h101", login="ok"),
        ),
        receipts=(cloud_receipt("live-a", 10, run_id="r-a"),),
    ).cloud
    assert [(a.name, a.cap, a.open_sessions) for a in result.accounts] == [
        ("h101", None, 0),
        ("h200", 17, 1),
    ]
    unread = floor(
        cloud_accounts=(ModelFloorCloudAccount(name="h200", login="ok", cap=17),),
    ).cloud
    assert unread.accounts[0].open_sessions is None


def test_a_signed_in_account_the_overlay_does_not_declare_is_counted_in_the_note_with_its_cause() -> (
    None
):
    result = floor(
        cloud_accounts=(
            ModelFloorCloudAccount(name="h200", login="ok", cap=14),
            ModelFloorCloudAccount(name="h203", login="ok", cap=10),
            ModelFloorCloudAccount(name="h101", login="no-claude-ai-login"),
        ),
    ).cloud
    assert [a.name for a in result.accounts] == ["h200"]
    assert "1 signed-in account" in result.note
    assert "not on the declared machine list" in result.note
    assert "1 listed machines hold no cloud login" in result.note


def test_the_bays_match_the_runner_units_count_and_a_refused_host_is_no_room() -> None:
    """OMN-22686: the 2026-11-06T14:27Z record drew 9 of 64 slots in use while the runner had no free slot.

    h053 had 14 placed units of which 9 were ledger lanes, at cap 16 and 2 free lane slots by the pool reading; h101 was
    refused by the reading and had a cap of 12; h201 had 1 free lane slot. The bays must add up to what the runner can place.
    """
    machines = (
        ModelFloorMachineDecl(host="h053", label="h053"),
        ModelFloorMachineDecl(host="h101", label="h101"),
        ModelFloorMachineDecl(host="h201", label="h201"),
    )
    ledger = tuple(
        r for n in range(9) for r in lane_rows(f"lane-{n}", host="h053")
    ) + lane_rows("lane-x", host="h201")
    result = compute_factory_floor(
        request(
            machines=machines,
            ledger=ledger,
            pool=(
                reading("h053", cap=16, lane_slots=2),
                reading(
                    "h101",
                    cap=12,
                    lane_slots=3,
                    refusal="PARITY-FAILED: bus-host: BUS-HOST-UNRESOLVED",
                ),
                reading("h201", cap=3, lane_slots=1),
            ),
            host_table=(
                ModelFloorHostRow(host="h053", cap=16),
                ModelFloorHostRow(host="h101", cap=12),
                ModelFloorHostRow(host="h201", cap=3),
            ),
            placed=(
                ModelFloorPlacedHost(host="h053", units=14, lanes=9),
                ModelFloorPlacedHost(host="h101", units=0, lanes=0),
                ModelFloorPlacedHost(host="h201", units=2, lanes=1),
            ),
        )
    ).floor
    bays = {b.host: (b.slots, b.running, b.free, b.stale) for b in result.bays}
    assert bays == {
        "h053": (16, 14, 2, False),
        "h101": (0, 0, 0, False),
        "h201": (3, 2, 1, False),
    }
    # The sum the headline draws is the sum of the runner's free lane slots (2 + 0 + 1), not the caps less the ledger lanes.
    assert sum(b.free for b in result.bays) == 3


# OMN-23081: the floor marked gitmut-0815, zeroflip-port-0830 and piece-readings-0815 alive but stalled while each
# lane's runner output file was written 09:16 to 09:17Z with tool calls in progress. Remote lanes write no ledger
# row between CLAIM and TERMINAL, so the stall reading saw only the CLAIM. Minutes below are the lanes' recorded
# ages read at 09:17:30Z: CLAIM 08:15 (62 min), 08:30 (47 min), 08:15 (62 min); output last written 09:17, 09:16, 09:16.
THREE_LANES = (
    ("gitmut-0815", 62, 0.5),
    ("zeroflip-port-0830", 47, 1.5),
    ("piece-readings-0815", 62, 1.5),
)


def three_lane_floor(*, with_output: bool, runner_alive: bool | None = True) -> Any:
    ledger: tuple[Any, ...] = ()
    receipts: tuple[Any, ...] = ()
    for lane, claim_ago, output_ago in THREE_LANES:
        ledger += lane_rows(lane, claim_ago=claim_ago, host="h051")
        receipts += (
            receipt(
                lane,
                started_at=ago(claim_ago),
                runner_alive=runner_alive,
                output_at=ago(output_ago) if with_output else None,
            ),
        )
    return floor(ledger=ledger, receipts=receipts)


def test_lanes_whose_runner_output_is_fresh_are_not_stalled() -> None:
    result = three_lane_floor(with_output=True)
    assert {lane.lane: lane.stalled for lane in result.lanes} == {
        "gitmut-0815": False,
        "zeroflip-port-0830": False,
        "piece-readings-0815": False,
    }
    assert {lane.lane: lane.silent_minutes for lane in result.lanes} == {
        "gitmut-0815": 0,
        "zeroflip-port-0830": 1,
        "piece-readings-0815": 1,
    }


def test_the_same_lanes_without_runner_output_are_still_stalled() -> None:
    # The failing control: the ledger alone, as the floor read it at 09:10:51Z, stalls all three.
    result = three_lane_floor(with_output=False)
    assert all(lane.stalled for lane in result.lanes)
    assert len(result.lanes) == 3


def test_runner_output_older_than_the_stall_bound_does_not_save_a_lane() -> None:
    result = floor(
        ledger=lane_rows("quiet-out", claim_ago=120, host="h051"),
        receipts=(receipt("quiet-out", started_at=ago(120), output_at=ago(45)),),
    )
    assert result.lanes[0].stalled is True
    assert result.lanes[0].silent_minutes == 45


def test_runner_output_of_a_finished_run_is_not_progress() -> None:
    result = floor(
        ledger=lane_rows("ended-out", claim_ago=120, host="h051"),
        receipts=(
            receipt(
                "ended-out",
                started_at=ago(120),
                output_at=ago(1),
                final=True,
                status="done",
            ),
        ),
    )
    assert result.lanes == ()


def test_runner_output_from_a_previous_run_is_not_progress_of_this_claim() -> None:
    result = floor(
        ledger=lane_rows("rerun", claim_ago=60, host="h051"),
        receipts=(receipt("rerun", started_at=ago(200), output_at=ago(1)),),
    )
    assert result.lanes[0].stalled is True
