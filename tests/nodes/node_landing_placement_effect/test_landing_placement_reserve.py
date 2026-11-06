# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A lab host keeps a reserved share of lane and worker admission while its own CI runner jobs drive load1 over the bar, and
the reserve never takes a host under its memory and swap floor (OMN-20864). Offline: every Reading is built by hand from the
measured figures.

THE INCIDENT. On 2026-10-11 n201 runs 44 self-hosted GitHub Actions runner containers beside the lanes. Between 05:45Z and
11:47Z the remote-lane runner refused 65 lane dispatches no-host; in 46 of them n201's refusal was LOAD-OVER-BAR, at load1
48.9-106.2 on 32 cores with 36.4-54.9 GB available and 5-45 runner jobs busy (10:35:48Z: load1 84.5, 45.7 GB available, 42
runner jobs busy). The load bar took n201's admission to zero, so the cause lanes that fix CI waited on CI."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_landing_placement_effect.handlers import (
    handler_landing_placement as lp,
)

ENGINES = ["claude_sonnet", "claude_opus"]


def host(name: str = "n201", **row: Any) -> Any:
    base = {"name": name, "engines": ENGINES}
    base.update(
        {"local": True} if name == "mac" else {"target": f"198.51.100.{name[1:]}"}
    )
    base.update(row)
    return lp.load_pool({"LANDING_POOL_HOSTS": json.dumps([base])})[0]


def n201(**over: Any) -> Any:
    """n201 at 2026-10-11T10:35:48Z, the reading in a no-host refusal: 42 runner jobs busy, load1 84.5 on 32 cores."""
    values: dict[str, Any] = {
        "cores": 32,
        "load1": 84.5,
        "busy_cores": 30.0,
        "mem_avail_gb": 45.7,
        "swap_used_frac": 0.35,
        "mem_psi_avg60": 2.4,
        "swap_in_kb_s": 3000.0,
        "swap_out_kb_s": 900.0,
        "cpu_temp_c": 79.0,
        "dimm_temp_c": 44.0,
        "placed": 0,
    }
    row = over.pop("row", {})
    values.update(over)
    return lp.Reading(host=host("n201", **row), **values)


def test_a_host_its_runner_jobs_hold_at_load1_84_still_admits_its_reserved_share() -> (
    None
):
    r = n201()
    assert r.load_bar_refusal is not None
    assert "LOAD-OVER-BAR" in r.load_bar_refusal
    assert r._free_slots(r.cap) == 0, (
        "over the bar the free-core arithmetic admits nothing"
    )
    assert r.reserve == lp.ADMIT_RESERVED_SLOTS == 2
    assert r.load_refusal is None
    assert r.admission_refusal is None
    assert r.lane_admission_refusal is None
    assert r.slots == 2
    assert r.lane_slots == 2


def test_the_reserve_holds_at_every_load_the_runner_jobs_reached() -> None:
    """The highest n201 load1 read in a refusal (10:30:47Z, 106.2, 45 runner jobs busy) still leaves the reserve open."""
    for load1 in (48.9, 84.5, 106.2, 150.0):
        assert n201(load1=load1).slots == 2, load1


def test_the_reserve_also_covers_a_host_under_the_bar_whose_free_cores_are_spent() -> (
    None
):
    """n201 at 2026-10-11T11:44Z: load1 43 on 32 cores, under the 1.5 bar but over the 1.0 ceiling, so no free core."""
    r = n201(load1=43.1, busy_cores=35.0, mem_avail_gb=56.0)
    assert r.load_bar_refusal is None
    assert r.free_cores < r.worker_cost_cores
    assert r.slots == 2


def test_over_the_bar_only_the_reserve_admits_whatever_the_busy_sample_says() -> None:
    """n202 on 2026-10-02: load1 59 on 32 cores read as five busy cores (iowait). The bar holds (OMN-20366): the busy sample
    must not reopen the free-core arithmetic beside the reserve."""
    r = n201(load1=59.0, busy_cores=5.0)
    assert r.free_cores // r.worker_cost_cores >= 13
    assert r.slots == 2
    assert r.lane_slots == 2


def test_placed_units_spend_the_reserve_and_the_refusal_says_so() -> None:
    assert n201(placed=1).slots == 1
    spent = n201(placed=2)
    assert spent.slots == 0
    assert spent.lane_slots == 0
    assert spent.load_refusal is not None
    assert "LOAD-OVER-BAR" in spent.load_refusal
    assert "RESERVE-HELD: 2 placed >= reserve 2" in spent.load_refusal
    assert spent.no_slot_reason == spent.admission_refusal


def test_a_host_under_the_memory_floor_admits_nothing_and_says_why() -> None:
    low = n201(mem_avail_gb=6.0)
    assert low.slots == 0
    assert low.lane_slots == 0
    assert "mem_avail 6.0G < 10G" in (low.admission_refusal or "")
    assert "RESERVE-MEMORY-FLOOR" in (low.admission_refusal or "")


def test_a_reserved_unit_is_charged_its_whole_memory_cap_above_the_floor() -> None:
    """A reserved unit may take the lane guard's full MemoryMax, and the host keeps the admission floor after it: one unit needs
    10 + 8 GB available, two need 26 GB. Under that the reserve is closed, so the load bar refuses with the reason."""
    assert lp.ADMIT_LANE_MEM_MAX_GB == 8.0
    assert lp.ADMIT_MIN_FREE_GB == 10.0
    assert n201(mem_avail_gb=17.9).slots == 0
    assert n201(mem_avail_gb=18.0).slots == 1
    assert n201(mem_avail_gb=26.0).slots == 2
    tight = n201(mem_avail_gb=15.0)
    assert tight.admission_refusal is not None
    assert "LOAD-OVER-BAR" in tight.admission_refusal
    assert (
        "RESERVE-MEMORY-FLOOR: 15.0G available leaves under the 10G floor after a reserved unit's 8G"
        in tight.admission_refusal
    )


def test_units_placed_inside_the_settle_window_are_charged_against_the_reserve_memory() -> (
    None
):
    """A unit placed seconds ago is not in mem_avail yet, so the reserve charges it its cap."""
    r = n201(mem_avail_gb=25.0, placed=1, fresh=1)
    assert r.slots == 0
    assert "RESERVE-MEMORY-FLOOR" in (r.load_refusal or "")


def test_swap_traffic_and_memory_psi_still_refuse_a_reserved_host() -> None:
    swapping = n201(swap_in_kb_s=81988.0, swap_out_kb_s=163532.0)
    assert swapping.slots == 0
    assert "swap traffic" in (swapping.admission_refusal or "")
    stalled = n201(mem_psi_avg60=5.2)
    assert stalled.slots == 0
    assert "memory PSI" in (stalled.admission_refusal or "")


def test_heat_still_refuses_a_reserved_host() -> None:
    hot = n201(cpu_temp_c=88.8)
    assert hot.slots == 0
    assert "cpu 88.8C > 85C" in (hot.admission_refusal or "")


def test_the_burst_budget_still_bounds_a_reserved_host() -> None:
    r = n201(placed=1, fresh=lp.PLACEMENT_BURST_PER_HOST)
    assert r.slots == 0
    assert "LOAD-OVER-BAR" in (r.load_refusal or "")


def test_the_table_row_sets_the_reserve_and_zero_turns_it_off() -> None:
    assert n201(row={"reserve": 0}).slots == 0
    assert (
        n201(row={"reserve": 0}).load_refusal
        == n201(row={"reserve": 0}).load_bar_refusal
    )
    assert n201(row={"reserve": 4}).slots == 4
    assert n201(row={"reserve": 4, "cap": 3}).slots == 3, (
        "the reserve never exceeds the host's cap"
    )


def test_the_shared_table_carries_the_reserve(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    table = tmp_path / "lab_run_hosts.yaml"
    table.write_text(
        "hosts:\n  - name: n201\n    target: 192.0.2.41\n    reserve: 3\n  - name: n202\n"
        "    target: 192.0.2.42\n",
        encoding="utf-8",
    )
    pool = {h.name: h for h in lp.load_pool({"ONEX_LAB_RUN_HOSTS": str(table)})}
    assert pool["n201"].reserve == 3
    assert pool["n202"].reserve is None


def test_the_launching_host_keeps_no_reserve_unless_its_row_names_one() -> None:
    mac = lp.Reading(host=host("mac"), cores=24, load1=60.0, mem_avail_gb=40.0)
    assert mac.reserve == 0
    assert mac.slots == 0
    assert mac.lane_slots == 0
    assert mac.load_refusal is not None


def test_the_reserve_never_lowers_what_the_free_core_arithmetic_admits() -> None:
    idle = n201(load1=4.0, busy_cores=4.0)
    assert idle.load_bar_refusal is None
    assert idle.slots > idle.reserve


def test_an_unreadable_host_takes_no_reserve() -> None:
    assert n201(error="rc=255 timed out after 20s").slots == 0


def test_the_landing_workers_on_a_host_do_not_spend_a_lanes_reserve() -> None:
    """At the 65 refusals n201 already held 3-16 placed units. A worker's reserve counts them all; a lane's counts only the lanes
    placed there, so landing workers never spend it."""
    worker = n201(placed=12)
    assert worker.slots == 0
    assert "RESERVE-HELD: 12 placed >= reserve 2" in (worker.admission_refusal or "")
    lane = n201(placed=12, lanes_placed=0)
    assert lane.lane_slots == 2
    assert lane.lane_admission_refusal is None
    assert n201(placed=12, lanes_placed=1).lane_slots == 1
    held = n201(placed=12, lanes_placed=2)
    assert held.lane_slots == 0
    assert "RESERVE-HELD: 2 lanes placed >= reserve 2" in (
        held.lane_admission_refusal or ""
    )


def test_a_full_host_takes_no_reserved_lane() -> None:
    full = n201(placed=16, lanes_placed=0)
    assert full.cap == 16
    assert full.lane_slots == 0
    assert "RESERVE-CAP-FULL: 16 placed >= cap 16" in (
        full.lane_admission_refusal or ""
    )
    assert n201(placed=15, lanes_placed=0).lane_slots == 1


def test_a_host_its_own_placements_overload_keeps_no_reserve() -> None:
    """n202 on 2026-10-02: load1 59 on 32 cores with 20 units placed. Crediting each placed unit two cores explains the load over
    the bar (59 - 40 = 19, not over 48), so the bar stands for lanes and workers alike (OMN-20366)."""
    for lanes in (None, 0):
        r = n201(
            load1=59.0, busy_cores=5.0, placed=20, lanes_placed=lanes, row={"cap": 28}
        )
        assert r.slots == 0
        assert r.lane_slots == 0
        assert "RESERVE-OWN-LOAD: the 20 units placed here explain the load" in (
            r.lane_admission_refusal or ""
        )


def test_lane_counts_counts_live_lane_markers_only(tmp_path: Path) -> None:
    import os

    live = os.getpid()
    for name in (
        f"n201.6248.{live}",
        f"n201.1791719083.{live}",
        f"n201.1791719187.{live}",
        "n201.1791719200.999999999",
        f"n202.1791719300.{live}",
        ".lock",
        ".limited-n101",
    ):
        (tmp_path / name).write_text("x", encoding="utf-8")
    assert lp.lane_counts(tmp_path) == {"n201": 2, "n202": 1}
    assert sorted(p.name for p in tmp_path.iterdir())[0] == ".limited-n101", (
        "read-only: nothing removed"
    )
    assert len(list(tmp_path.iterdir())) == 7
    assert lp.lane_counts(tmp_path / "absent") == {}


def test_a_placement_record_says_when_it_was_admitted_on_the_reserve() -> None:
    r = n201()
    record = lp.Placement(
        host=r.host, engine="claude_opus", reading=r, readings=(r,)
    ).record()
    assert record["reserve"] is True
    calm = n201(load1=4.0, busy_cores=4.0)
    assert (
        lp.Placement(
            host=calm.host, engine="claude_opus", reading=calm, readings=(calm,)
        ).record()["reserve"]
        is False
    )


def n202(**over: Any) -> Any:
    """n202 in the placement record of lease 10927 (2026-10-18T21:12Z): no Claude login, a Codex login whose sandbox writes,
    cap 8, one worker placed, seven slots free."""
    values: dict[str, Any] = {
        "cores": 32,
        "load1": 3.2,
        "busy_cores": 1.2,
        "mem_avail_gb": 33.7,
        "placed": 1,
        "codex_login": True,
        "codex_sandbox": "ok",
    }
    values.update(over)
    return lp.Reading(host=host("n202", engines=[], cap=8, codex=True), **values)
