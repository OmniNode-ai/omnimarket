# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Focused placement, pool policy and remote worker tests (OMN-19998)."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_landing_placement_effect.handlers import (
    handler_landing_placement as lp,
)

# The default pool (Claude hosts only) plus two lab hosts that hold no Claude login, as rows a
# LANDING_POOL_HOSTS override could carry.
# A lab host row with a Claude login, as the lab host table fixture declares it (documentation-range address).
FIXTURE_HOST_ROW = {
    "name": "n201",
    "target": "192.0.2.41",
    "engines": list(lp.CLAUDE_ENGINES),
    "gh": True,
}
HOSTS = {
    host.name: host
    for host in lp.load_pool(
        {
            "LANDING_POOL_HOSTS": json.dumps(
                [
                    # A full host here is load over its free cores with nothing placed, so the lab rows keep no reserved
                    # share (OMN-20864, covered by tests/test_landing_placement_reserve.py).
                    lp.LOCAL_POOL_ROW,
                    {**FIXTURE_HOST_ROW, "reserve": 0},
                    {
                        "name": "n101",
                        "target": "192.0.2.31",
                        "engines": [],
                        "cap": 2,
                        "reserve": 0,
                    },
                    {
                        "name": "n105",
                        "target": "192.0.2.35",
                        "engines": [],
                        "cap": 2,
                        "reserve": 0,
                    },
                ]
            )
        }
    )
}


def reading(
    name: str, cores: int, load1: float, memory: float = 57.0, placed: int = 0
) -> Any:
    return lp.Reading(
        HOSTS[name], cores=cores, load1=load1, mem_avail_gb=memory, placed=placed
    )


def brief() -> dict[str, Any]:
    return {
        "short": "omnibase_infra#42",
        "head_ref": "feat/x",
        "brief": {
            "pr": "OmniNode-ai/omnibase_infra#42",
            "lease_id": 7,
            "engine": "claude_sonnet",
            "head_sha": "a" * 40,
            "deadline_at": "2026-09-29T18:00:00Z",
            "brief_class": "product_red",
            "instructions": "fix it",
            "push_rule": "push",
            "result_rule": "result",
        },
    }


def no_probe(*args: Any, **kwargs: Any) -> None:
    pytest.fail("Disabled placement must never probe a host")


def test_live_readings_choose_h201_for_claude_and_exclude_overloaded_mac() -> None:
    readings = [
        reading("mac", 24, 100),
        reading("n201", 32, 4.3),
        reading("n101", 12, 3.2, 6.9),
        reading("n105", 10, 2.2, 9.7),
    ]
    # The Mac's ceiling is 0.75 per core: 18 - 100.
    assert readings[0].slots == 0
    assert readings[0].free_cores == -82
    assert readings[1].free_cores == pytest.approx(27.7)
    for engine in ("claude_sonnet", "claude_opus"):
        pick = lp.choose(engine, readings)
        assert pick is not None
        assert (pick.host.name, pick.engine) == ("n201", "claude_sonnet")


def test_mac_is_eligible_when_it_has_more_headroom_than_h201() -> None:
    pick = lp.choose("claude_sonnet", [reading("mac", 24, 3), reading("n201", 32, 30)])
    assert pick is not None
    assert (pick.host.name, pick.engine) == ("mac", "claude_sonnet")


def test_claude_opus_is_not_placed_when_claude_hosts_are_full() -> None:
    """Codex drafts inside workers; an idle host without a Claude login takes no worker."""
    readings = [
        reading("mac", 24, 100),
        reading("n201", 32, 40),
        reading("n101", 12, 0),
    ]
    assert lp.choose("claude_opus", readings) is None


def test_memory_below_floor_prevents_placement() -> None:
    # The controller host is judged by the slot floor alone (MIN_FREE_GB): at it an idle host still takes one,
    # and each WORKER_MEM_GB above it adds one.
    low = reading("mac", 24, 0, lp.MIN_FREE_GB - 0.1)
    assert low.slots == 0
    assert lp.choose("claude_opus", [low]) is None
    assert replace(low, mem_avail_gb=lp.MIN_FREE_GB).slots == 1
    assert replace(low, mem_avail_gb=lp.MIN_FREE_GB + 3 * lp.WORKER_MEM_GB).slots == 3
    # A lab host is also held to the admission floor (OMN-20207): under ADMIT_MIN_FREE_GB it takes none.
    lab = reading("n201", 32, 0, lp.ADMIT_MIN_FREE_GB - 0.1)
    assert lab.slots == 0
    assert lp.choose("claude_opus", [lab]) is None
    assert replace(lab, mem_avail_gb=lp.ADMIT_MIN_FREE_GB).slots > 0


def test_unreadable_hosts_are_excluded_and_probe_parser_reads_capacity() -> None:
    unreadable = replace(reading("n201", 32, 0), error="unreachable")
    assert unreadable.slots == 0
    assert unreadable.free_cores == float("-inf")
    assert lp.choose("claude_opus", [unreadable]) is None
    assert lp.choose("claude_sonnet", [unreadable]) is None
    garbage = lp.parse_probe(HOSTS["n201"], "garbage", 2)
    assert garbage.error is not None
    assert garbage.slots == 0
    assert garbage.placed == 2
    parsed = lp.parse_probe(HOSTS["n201"], "PROBE 32 4.27 58720256", 1)
    assert parsed.error is None
    assert parsed.cores == 32
    assert parsed.placed == 1
    assert parsed.load1 == pytest.approx(4.27)
    assert parsed.mem_avail_gb == pytest.approx(56)


def test_read_pool_uses_local_shell_remote_ssh_and_reports_nonzero_rc() -> None:
    calls: list[list[str]] = []

    def capture(argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        assert timeout == {
            "/bin/sh": lp.LOCAL_PROBE_TIMEOUT_S,
            "ping": lp.link_gate.LINK_PING_TIMEOUT_S,
        }.get(argv[0], lp.PROBE_TIMEOUT_S)
        rc = 255 if HOSTS["n101"].target in argv else 0
        return subprocess.CompletedProcess(
            argv, rc, "PROBE 32 4.27 58720256", "unreachable" if rc else ""
        )

    readings = lp.read_pool(
        [HOSTS[n] for n in ("mac", "n201", "n101")],
        placed={"n201": 1, "n101": 2},
        capture=capture,
        lane="test-lane",
    )
    pings = [argv[-1] for argv in calls if argv[0] == "ping"]
    assert sorted(pings) == sorted([HOSTS["n201"].target, HOSTS["n101"].target]), (
        "the local host is never pinged"
    )
    calls = [argv for argv in calls if argv[0] != "ping"]
    assert len(calls) == 3
    local = next(argv for argv in calls if argv[0] == "/bin/sh")
    assert local[1] == "-c"
    assert "PROBE" in local[2]
    remote = [argv for argv in calls if argv[0] == "ssh"]
    assert {argv[-2] for argv in remote} == {HOSTS["n201"].target, HOSTS["n101"].target}
    assert all(
        argv[-1].splitlines()[0] == "# lane=test-lane placement-probe"
        for argv in remote
    )
    assert readings[0].error is None
    assert readings[1].placed == 1
    assert readings[1].error is None
    assert readings[2].placed == 2
    assert readings[2].slots == 0
    assert "rc=255 unreachable" in readings[2].error


def test_placed_counts_keeps_live_markers_and_removes_dead_pids(tmp_path: Path) -> None:
    directory = tmp_path / "placement"
    assert lp.placed_counts(directory) == {}
    live = lp.mark(directory, "n201", 7, os.getpid())
    other = lp.mark(directory, "n201", 8, os.getpid())
    dead = lp.mark(directory, "n101", 9, 999999)
    assert live.read_text(encoding="utf-8") == str(os.getpid())
    assert lp.placed_counts(directory) == {"n201": 2}
    assert live.exists()
    assert other.exists()
    assert not dead.exists()


def test_placed_counts_never_removes_the_placement_lock(tmp_path: Path) -> None:
    """The lock file lives in the marker directory. Unlinking it as a malformed marker let five
    workers started in one tick each lock a fresh inode, all read placed=0, and all take n201 past
    its cap of 3 (2026-09-29T20:19Z, leases 74-78)."""
    directory = tmp_path / "placement"
    directory.mkdir()
    lock = directory / ".lock"
    lock.write_text("", encoding="utf-8")
    lp.mark(directory, "n201", 7, os.getpid())
    assert lp.placed_counts(directory) == {"n201": 1}
    assert lock.exists()


def test_placed_counts_keeps_the_state_files_the_placement_directory_holds(
    tmp_path: Path,
) -> None:
    """The parity records queue-status reads and the gh fallback episodes live in the marker directory. ``placed_counts`` unlinked
    every file that was not a slot marker, so the next placement deleted them: queue-status printed "no record" (2026-10-17T18:05Z,
    after "OK hosts=4" at 17:07Z) and a gh fallback episode began again with a second FRICTION row."""
    directory = tmp_path / "placement"
    directory.mkdir()
    state = {
        lp.PARITY_FILE: '{"hosts": {"n201": {"host": "n201"}}}',
        lp.GH_FALLBACK_FILE: "{}",
    }
    for name, body in state.items():
        (directory / name).write_text(body, encoding="utf-8")
    live = lp.mark(directory, "n201", 7, os.getpid())
    dead = lp.mark(directory, "n101", 9, 999999)
    assert lp.placed_counts(directory) == {"n201": 1}
    assert live.exists()
    assert not dead.exists()
    assert {
        name: (directory / name).read_text(encoding="utf-8") for name in state
    } == state
    assert [r["host"] for r in lp.recorded_parity(directory)] == ["n201"]


def test_load_pool_defaults_and_explicit_empty_invalid_or_targetless_rows() -> None:
    hosts = lp.load_pool(
        {"ONEX_SKILL_OVERLAY_ROOTS": os.environ["ONEX_SKILL_OVERLAY_ROOTS"]}
    )
    assert [host.name for host in hosts] == ["mac", "n201"]
    assert hosts[0].local
    assert hosts[0].target is None
    assert all(not host.local for host in hosts[1:])
    for raw in (
        "[]",
        "not json",
        json.dumps([{"name": "remote", "engines": ["codex_high"]}]),
    ):
        assert lp.load_pool({"LANDING_POOL_HOSTS": raw}) == []


def _full_pool_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, deadline: str
) -> tuple[Path, dict[str, Any]]:
    lease = tmp_path / "lease"
    lease.mkdir()
    payload = brief()
    payload["lease_id"] = payload["brief"]["lease_id"]
    payload["brief"]["deadline_at"] = deadline
    (lease / "brief.json").write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv(
        "LANDING_POOL_HOSTS", json.dumps([{**FIXTURE_HOST_ROW, "reserve": 0}])
    )
    monkeypatch.setenv("LANDING_PLACEMENT_DIR", str(tmp_path / "placement"))
    return lease, payload


def test_a_linux_host_busy_on_io_wait_not_cpu_takes_workers_by_its_busy_cores() -> None:
    """n202 on 2026-09-30 read load 30 on 32 cores with 60% of its CPU idle: load1 counts tasks in IO
    wait. The probe's BUSY line (cores busy over a one-second sample) replaces load1 when lower."""
    host = HOSTS["n201"]
    text = "PROBE 32 30.0 33554432\nBUSY 6.40\nCOAUTHOR false\nLOGIN 1 1"
    parsed = lp.parse_probe(host, text, 3)
    assert parsed.busy_cores == pytest.approx(6.4)
    assert parsed.effective_load1 == 6.4
    # 32 - 6.4 = 25.6 free cores // 2 = 12 (the 3 placed are settled, so in the load already); load1 alone would leave 32 - 30 = 2.
    assert parsed.free_cores == pytest.approx(25.6)
    assert parsed.slots == 12
    assert "load1=30.0,busy=6.4" in parsed.describe()
    blind = lp.parse_probe(host, "PROBE 32 30.0 33554432\nCOAUTHOR false\nLOGIN 1 1", 3)
    assert blind.busy_cores is None
    assert blind.free_cores == pytest.approx(2.0)
    assert blind.slots == 1
    # A CPU-bound host is not flattered: busy above load1 never raises the load.
    hot = lp.parse_probe(
        host, "PROBE 32 20.0 33554432\nBUSY 31.5\nCOAUTHOR false\nLOGIN 1 1", 0
    )
    assert hot.effective_load1 == 20.0
    # A malformed BUSY line is ignored, never an error.
    assert (
        lp.parse_probe(
            host, "PROBE 32 4.0 33554432\nBUSY x\nCOAUTHOR false\nLOGIN 1 1", 0
        ).busy_cores
        is None
    )


def test_a_lab_host_charges_two_cores_a_worker_and_the_controller_mac_four() -> None:
    laptop = replace(HOSTS["n101"], cap=None)
    # 10 cores at load 3.9 with two workers placed (the 20:37Z REMOTE-LANE-NO-HOST reading of n105).
    lab = lp.Reading(laptop, cores=10, load1=3.9, mem_avail_gb=11.8, placed=2)
    assert lab.cap == 5
    assert lab.slots == 3
    mac = lp.Reading(HOSTS["mac"], cores=10, load1=1.0, mem_avail_gb=11.8, placed=0)
    assert mac.cap == 2


# ---------------------------------------------------------------------------------------------------
# OMN-20199: lane admission on memory and swap. The remote-lane runner applies the bar in its own eligibility,
# and the landing slot arithmetic honors the same refusal (OMN-20207: a refused host gets 0 slots).
# ---------------------------------------------------------------------------------------------------


def _lab(
    mem: float,
    swap: float | None = None,
    local: bool = False,
    pressure: int | None = None,
    name: str = "n202",
    psi: float | None = None,
) -> Any:
    host = lp.Host(
        name="mac" if local else name,
        target=None if local else "198.51.100.202",
        engines=frozenset({"claude_sonnet"}),
        cap=None,
        gh=True,
    )
    return lp.Reading(
        host,
        cores=32,
        load1=1.0,
        mem_avail_gb=mem,
        swap_used_frac=swap,
        mem_pressure=pressure,
        mem_psi_avg60=psi,
    )


def test_admission_rule_is_declared_as_data() -> None:
    assert lp.ADMIT_LANE_MEM_MAX_GB == 8.0
    assert lp.ADMIT_HEADROOM_GB == 2.0
    assert (
        lp.ADMIT_MIN_FREE_GB == lp.ADMIT_LANE_MEM_MAX_GB + lp.ADMIT_HEADROOM_GB == 10.0
    )
    assert lp.ADMIT_MAX_MEM_PSI == 5.0
    assert lp.ADMIT_MAX_SWAP_KB_S == 100000
    assert lp.ADMIT_MAC_CRITICAL_PRESSURE == 4
    assert lp.ADMIT_MAC_WARN_PRESSURE == 2
    assert lp.ADMIT_MAC_MIN_FREE_PCT == 10.0
    assert lp.ADMIT_MAC_WARN_MARGIN_GB == 8.0
    assert not hasattr(lp, "ADMIT_SWAP_FULL_USED"), "one swap rule"
    assert not hasattr(lp, "ADMIT_SWAP_FULL_LOW_FREE_GB"), "one swap rule"


# The 2026-09-30T22:25Z census: swap percent refused every host but n201, though each had memory to give.
CENSUS = [
    ("n202", {"mem": 39.0, "swap": 0.86, "psi": 0.41}, None),
    ("n201", {"mem": 60.0, "swap": 0.14, "psi": 0.0}, None),
    (
        "n101",
        {"mem": 30.0, "swap": None, "pressure": 1},
        None,
    ),  # macOS, swap 87% in the old read, pressure normal
    (
        "n105",
        {"mem": 24.0, "swap": None, "pressure": 1},
        None,
    ),  # macOS, swap 78%, pressure normal
]


@pytest.mark.parametrize(("name", "kw", "_"), CENSUS)
def test_the_census_hosts_with_memory_and_no_pressure_are_admitted(
    name: str, kw: dict[str, Any], _: Any
) -> None:
    assert _lab(name=name, **kw).admission_refusal is None


def test_a_linux_host_with_swap_in_use_under_memory_pressure_is_refused() -> None:
    # The n201 runaway shape: 89 of 91 GB used, 199 of 199 GB swap, tasks stalled on memory. Available memory here is
    # above the floor so that PSI alone refuses.
    why = _lab(20.0, 0.99, name="n201", psi=40.0).admission_refusal or ""
    assert "memory PSI 40.0% > 5%" in why
    assert _lab(39.0, 0.99, psi=0.41).admission_refusal is None, (
        "full swap with no pressure is cold pages"
    )
    assert "memory PSI 40.0% > 5%" in (
        _lab(20.0, 0.20, psi=40.0).admission_refusal or ""
    ), "PSI alone refuses"
    assert _lab(9.0, 0.0).admission_refusal is not None, (
        "the memory floor still refuses alone"
    )


def test_a_mac_is_judged_on_memory_pressure_never_on_swap() -> None:
    assert _lab(30.0, 0.87, pressure=1).admission_refusal is None
    # Warn is admitted while the free memory covers a lane plus the margin (OMN-19998); critical refuses whatever is free.
    assert _lab(30.0, 0.87, pressure=2).admission_refusal is None
    assert "macOS memory pressure warn (level 2) and free ~12.0G" in (
        _lab(12.0, 0.87, pressure=2).admission_refusal or ""
    )
    assert "macOS memory pressure critical (level 4 >= 4): free ~30.0G" in (
        _lab(30.0, None, pressure=4).admission_refusal or ""
    )


def test_the_mac_probe_prints_a_pressure_line() -> None:
    mac_branch = lp._PROBE.split("else", 1)[1].split("fi", 1)[0]
    assert "kern.memorystatus_vm_pressure_level" in mac_branch
    assert 'echo "PRESSURE $q"' in lp._PROBE
    parsed = lp.parse_probe(
        _lab(0).host,
        "PROBE 10 1.0 50331648\nPRESSURE 4\nCOAUTHOR false\nLOGIN 1 1\n",
        0,
    )
    assert parsed.mem_pressure == 4
    assert (
        "ADMISSION-REFUSED(macOS memory pressure critical (level 4 >= 4)"
        in parsed.describe()
    )
    assert "pressure=4" in parsed.describe()
    assert (
        lp.parse_probe(
            _lab(0).host, "PROBE 10 1.0 50331648\nCOAUTHOR false\nLOGIN 1 1\n", 0
        ).mem_pressure
        is None
    )


def test_a_host_with_no_swap_reading_or_no_swap_is_judged_on_memory_alone() -> None:
    assert _lab(64.0, None).admission_refusal is None
    parsed = lp.parse_probe(
        _lab(0).host, "PROBE 32 1.0 67108864\nSWAP 0 0\nCOAUTHOR false\nLOGIN 1 1\n", 0
    )
    assert parsed.swap_used_frac is None
    assert parsed.admission_refusal is None


def test_the_local_host_and_an_unreadable_host_are_not_judged_by_the_lane_bar() -> None:
    assert _lab(2.0, 0.9, local=True).admission_refusal is None
    assert lp.Reading(_lab(0).host, error="ssh down").admission_refusal is None


def test_probe_swap_line_is_parsed_into_the_fraction_in_use() -> None:
    text = "PROBE 32 1.0 50331648\nBUSY 3.00\nSWAP 8388608 2097152\nMEMPSI 0.00\nCOAUTHOR false\nLOGIN 1 1\n"
    reading_ = lp.parse_probe(_lab(0).host, text, 0)
    assert reading_.swap_used_frac == pytest.approx(0.75)
    assert reading_.mem_avail_gb == pytest.approx(48.0)
    assert "swap=75%" in reading_.describe()
    assert reading_.admission_refusal is None
    assert (
        lp.parse_probe(
            _lab(0).host, "PROBE 32 1.0 50331648\nCOAUTHOR false\nLOGIN 1 1\n", 0
        ).swap_used_frac
        is None
    )


def test_the_linux_probe_prints_a_swap_line_and_the_mac_probe_does_not() -> None:
    assert "SwapTotal" in lp._PROBE
    assert 'echo "SWAP $w"' in lp._PROBE
    mac_branch = lp._PROBE.split("else", 1)[1].split("fi", 1)[0]
    assert "SwapTotal" not in mac_branch


def test_a_host_the_admission_rule_refuses_gets_zero_slots() -> None:
    """OMN-20207: the controller kept placing workers on n101 while its own probe line read the admission refusal
    (mem_avail 7.0G < 10G, macOS memory pressure level 2), and those workers could not push (OMN-20216)."""
    n101 = lp.Reading(
        replace(HOSTS["n101"], cap=None),
        cores=10,
        load1=0.5,
        mem_avail_gb=7.0,
        mem_pressure=2,
    )
    why = n101.admission_refusal or ""
    assert "mem_avail 7.0G < 10G" in why
    assert "macOS memory pressure warn (level 2) and free ~7.0G" in why
    assert n101.slots == 0
    assert "slots=0" in n101.describe()
    assert "ADMISSION-REFUSED" in n101.describe()
    # Each refusal reason alone takes the slots, with cores and memory to spare.
    assert replace(n101, mem_avail_gb=30.0, mem_pressure=4).slots == 0, (
        "critical pressure alone"
    )
    assert replace(n101, mem_avail_gb=12.0).slots == 0, (
        "warn with under a lane plus margin free alone"
    )
    assert replace(n101, mem_avail_gb=9.9, mem_pressure=1).slots == 0, (
        "the 10 GB floor alone"
    )
    assert _swapped(psi=12.5).slots == 0, "Linux memory PSI over the bar"
    # The same host, admitted, keeps its slots.
    admitted = replace(n101, mem_avail_gb=30.0, mem_pressure=1)
    assert admitted.admission_refusal is None
    assert admitted.slots > 0
    # An idle refused host does not get the idle host's one free slot either.
    assert n101.idle
    assert n101.slots == 0
    # The controller host and an unreadable host are not judged by the lane bar (slots are as before).
    assert (
        lp.Reading(
            HOSTS["mac"], cores=10, load1=1.0, mem_avail_gb=7.0, mem_pressure=2
        ).slots
        > 0
    )


def test_the_pool_places_nothing_on_a_refused_host_and_the_others_are_unchanged() -> (
    None
):
    refused = lp.Reading(
        replace(HOSTS["n101"], cap=None, engines=frozenset({"claude_sonnet"})),
        cores=10,
        load1=0.5,
        mem_avail_gb=7.0,
        mem_pressure=2,
    )
    others = [
        lp.Reading(
            replace(HOSTS["n105"], engines=frozenset({"claude_sonnet"})),
            cores=10,
            load1=1.0,
            mem_avail_gb=24.0,
            mem_pressure=1,
        ),
        lp.Reading(
            HOSTS["n201"], cores=32, load1=4.0, mem_avail_gb=60.0, mem_psi_avg60=0.0
        ),
    ]
    before = [r.slots for r in others]
    assert all(n > 0 for n in before)
    pool = [refused, *others]
    assert [r.slots for r in pool] == [0, *before]
    assert lp.pool_slots(pool) == sum(before)
    placement = lp.choose("claude_sonnet", pool)
    assert placement is not None
    assert placement.host.name != "n101"
    assert lp.choose("claude_sonnet", [refused]) is None


# ---------------------------------------------------------------------------------------------------
# OMN-17427 (lane idle-hosts-diag), updated by OMN-20843: live PSI or sustained swap traffic refuses a lane.
# n202 on 2026-09-30 at 22:25Z: 7.6 GB swap 96% occupied by cold pages, 44 GB available, no swap-outs, PSI
# memory some avg60 0.09, and the occupancy rule left the lane runner one lab host.
# ---------------------------------------------------------------------------------------------------


def _swapped(
    psi: float | None = None,
    si: float | None = None,
    so: float | None = None,
    swap: float = 0.96,
    mem: float = 40.0,
) -> Any:
    return replace(
        _lab(mem, swap), mem_psi_avg60=psi, swap_in_kb_s=si, swap_out_kb_s=so
    )


def test_full_swap_with_no_memory_pressure_admits_the_host() -> None:
    assert lp.ADMIT_MAX_MEM_PSI == 5.0
    assert _swapped(psi=0.09).admission_refusal is None, (
        "cold pages in swap are not pressure"
    )
    assert _swapped(psi=5.0).admission_refusal is None, "the PSI bar is inclusive"
    assert _swapped(si=0, so=0).admission_refusal is None, (
        "no PSI: quiet swap traffic admits"
    )


def test_full_swap_under_memory_pressure_is_refused_and_says_why() -> None:
    by_psi = _swapped(psi=12.5).admission_refusal or ""
    assert "memory PSI 12.5% > 5%" in by_psi
    by_traffic = _swapped(si=20000, so=90000).admission_refusal or ""
    assert "swap traffic 110000 KB/s (in 20000, out 90000) > 100000 KB/s" in by_traffic


def test_psi_and_swap_traffic_decide_independently_and_occupancy_alone_never_refuses() -> (
    None
):
    assert _swapped(psi=0.5, si=0, so=40).admission_refusal is None, (
        "low PSI and quiet traffic admit"
    )
    assert _swapped(psi=0.5, si=20000, so=90000).admission_refusal is not None, (
        "low PSI does not mask swap traffic"
    )
    assert _swapped().admission_refusal is None, (
        "an older probe's occupancy alone never refuses"
    )


def test_memory_floor_still_refuses_whatever_the_pressure() -> None:
    assert "mem_avail 7.3G < 10G" in (
        _swapped(psi=0.0, mem=7.3).admission_refusal or ""
    )


def test_probe_mempsi_and_swapio_lines_are_parsed() -> None:
    text = "PROBE 32 5.1 41943040\nBUSY 2.30\nSWAP 7864320 314572\nSWAPIO 4 0 0 0 0\nMEMPSI 0.09\nCOAUTHOR false\nLOGIN 1 1\n"
    reading_ = lp.parse_probe(_lab(0).host, text, 2)
    assert reading_.mem_psi_avg60 == pytest.approx(0.09)
    assert (
        reading_.swap_in_kb_s,
        reading_.swap_out_kb_s,
    ) == (0.0, 0.0)
    assert reading_.admission_refusal is None
    assert "mempsi=0.09" in reading_.describe()
    bare = lp.parse_probe(
        _lab(0).host,
        "PROBE 32 1.0 50331648\nSWAP 8388608 0\nCOAUTHOR false\nLOGIN 1 1\n",
        0,
    )
    assert bare.mem_psi_avg60 is None
    assert bare.swap_in_kb_s is None
    assert bare.swap_out_kb_s is None
    assert bare.admission_refusal is None


def test_the_linux_probe_reads_psi_and_both_probes_read_swap_traffic() -> None:
    assert "/proc/pressure/memory" in lp._PROBE
    assert "pswpin" in lp._PROBE
    assert "pswpout" in lp._PROBE
    assert 'echo "MEMPSI $k"' in lp._PROBE
    assert 'echo "SWAPIO $sio"' in lp._PROBE
    mac_branch = lp._PROBE.split("else", 1)[1].split("fi", 1)[0]
    assert "/proc/pressure" not in mac_branch
    assert "pswp" not in mac_branch
    assert "Swapins" in mac_branch
    assert "Swapouts" in mac_branch


# ---------------------------------------------------------------------------------------------------
# OMN-20843: the memory bar judges live measures only. Swap occupancy is history (pages pushed out long ago), so it neither
# refuses a host nor keeps a pressed one admitted: memory PSI refuses on its own, and so does swap traffic (swap-in plus
# swap-out, KB/s) over ADMIT_MAX_SWAP_KB_S in both of the probe's two consecutive one-second samples. n201 at 2026-10-10T00:28Z:
# swap 62 of 199 GB in use, PSI some avg60 2.19, swap traffic about 31 MB/s on average over 90 one-second samples.
# ---------------------------------------------------------------------------------------------------


def _live(
    swap: float | None = 0.32,
    psi: float | None = None,
    si: float | None = None,
    so: float | None = None,
    pressure: int | None = None,
) -> Any:
    return replace(
        _lab(54.0, swap, pressure=pressure, name="n201", psi=psi),
        swap_in_kb_s=si,
        swap_out_kb_s=so,
    )


def test_live_memory_bar_is_declared_once_as_data() -> None:
    assert lp.ADMIT_MAX_MEM_PSI == 5.0
    assert lp.ADMIT_MAX_SWAP_KB_S == 100000
    assert not hasattr(lp, "ADMIT_MAX_SWAP_USED"), "swap occupancy is not a bar"


def test_live_memory_refuses_on_swap_in_alone_with_low_psi() -> None:
    reading_ = _live(psi=1.70, si=25000.0, so=0.0)
    assert "swap-in 25000 KB/s > 20000 KB/s" in (reading_.admission_refusal or "")
    assert reading_.slots == 0


def test_live_memory_admits_at_the_swap_in_bar_and_h201_quiet_traffic() -> None:
    assert _live(si=20000.0, so=0.0).admission_refusal is None, (
        "the swap-in bar is inclusive"
    )
    assert _live(psi=2.19, si=12670.0, so=18216.0).admission_refusal is None


def test_live_memory_admits_stale_swap_with_low_psi_and_quiet_swap_traffic() -> None:
    assert _live(swap=0.32, psi=1.0, si=40.0, so=0.0).admission_refusal is None
    assert _live(swap=0.99, psi=0.2, si=0.0, so=0.0).admission_refusal is None
    assert _live(swap=0.99, psi=None, si=0.0, so=0.0).admission_refusal is None, (
        "no PSI: swap traffic decides"
    )
    assert _live(swap=0.99, psi=None, si=None, so=None).admission_refusal is None, (
        "occupancy alone never refuses"
    )
    assert (
        _live(swap=0.32, psi=2.19, si=20000.0, so=80000.0).admission_refusal is None
    ), "the traffic bar is inclusive"


def test_live_memory_refuses_on_psi_alone_whatever_the_swap_occupancy() -> None:
    for swap in (None, 0.0, 0.20, 0.32, 0.99):
        assert "memory PSI 7.6% > 5%" in (
            _live(swap=swap, psi=7.6, si=0.0, so=0.0).admission_refusal or ""
        ), swap


def test_live_memory_refuses_sustained_swap_traffic_whatever_the_psi() -> None:
    why = _live(swap=0.10, psi=0.5, si=20000.0, so=90000.0).admission_refusal or ""
    assert "swap traffic 110000 KB/s (in 20000, out 90000) > 100000 KB/s" in why
    assert (
        _live(swap=None, psi=None, si=0.0, so=120000.0).admission_refusal is not None
    ), "no PSI read"
    assert (
        _live(swap=None, pressure=1, si=0.0, so=120000.0).admission_refusal is not None
    ), "a Mac too"
    assert _live(psi=0.5, si=20000.0, so=90000.0).slots == 0


def test_live_memory_refusal_text_names_the_live_measure_never_occupancy() -> None:
    why = _live(swap=0.99, psi=40.0, si=60000.0, so=70000.0).admission_refusal or ""
    assert "memory PSI 40.0% > 5%" in why
    assert "swap traffic 130000 KB/s" in why
    assert "in use" not in why
    assert "99%" not in why


def test_swapio_line_is_parsed_into_the_lower_of_two_samples_in_kb_per_second() -> None:
    text = (
        "PROBE 32 14.4 56523000\nSWAP 209715188 143866144\nSWAPIO 4 3000 18260 1000 500\nMEMPSI 0.17\n"
        "COAUTHOR false\nLOGIN 1 1\n"
    )
    r = lp.parse_probe(_lab(0).host, text, 0)
    assert (r.swap_in_kb_s, r.swap_out_kb_s) == (4000.0, 2000.0), (
        "the quieter sample is what was sustained"
    )
    assert r.admission_refusal is None
    assert "si=4000KB/s,so=2000KB/s" in r.describe()
    burst = lp.parse_probe(
        _lab(0).host,
        text.replace("SWAPIO 4 3000 18260 1000 500", "SWAPIO 4 30000 0 1 1"),
        0,
    )
    assert burst.admission_refusal is None, "one burst second is not sustained"
    mac = lp.parse_probe(
        _lab(0).host,
        "PROBE 10 1.0 50331648\nSWAPIO 16 4000 4000 5000 3000\nPRESSURE 1\nLOGIN 1 1\n",
        0,
    )
    assert (mac.swap_in_kb_s, mac.swap_out_kb_s) == (64000.0, 64000.0)
    assert "swap traffic 128000 KB/s (in 64000, out 64000) > 100000 KB/s" in (
        mac.admission_refusal or ""
    )
    for bad in (
        "SWAPIO 4 1 2 3",
        "SWAPIO 0 1 2 3 4",
        "SWAPIO x 1 2 3 4",
        "SWAPIO 4 -1 2 3 4",
    ):
        r = lp.parse_probe(
            _lab(0).host, f"PROBE 32 1.0 56523000\n{bad}\nLOGIN 1 1\n", 0
        )
        assert r.swap_in_kb_s is None, bad
        assert r.swap_out_kb_s is None, bad


def test_swapio_round_trips_through_a_cached_description() -> None:
    r = _live(psi=0.5, si=20000.0, so=90000.0)
    parsed = lp.Reading.from_description(r.host, r.describe())
    assert parsed is not None
    assert parsed.admission_refusal == r.admission_refusal
    assert (parsed.swap_in_kb_s, parsed.swap_out_kb_s) == (20000.0, 90000.0)


def test_swapio_is_read_by_both_probes_over_two_one_second_samples() -> None:
    linux, mac = (
        lp._PROBE.split("else", 1)[0],
        lp._PROBE.split("else", 1)[1].split("\nfi\n", 1)[0],
    )
    assert "pswpin" in linux
    assert "pswpout" in linux
    assert "getconf PAGESIZE" in linux
    assert "Swapins" in mac
    assert "Swapouts" in mac
    assert "pswp" not in mac
    assert 'echo "SWAPIO $sio"' in lp._PROBE
    assert "SWAPOUT" not in lp._PROBE


# ---------------------------------------------------------------------------------------------------
# OMN-20205 (lane admission-reconcile): one admission rule. The floor is the lane's own memory cap (the lane guard's
# MemoryMax) plus headroom, not a separate number, and a Mac at pressure level warn takes no lane.
# ---------------------------------------------------------------------------------------------------


# The 2026-09-30T22:33Z live readback (lane idle-hosts-diag): n201 and n202 admitted under the PSI rule; n105 with 11.6 GB
# free at pressure normal and n101 with 7.3 GB free at pressure warn were both refused by the 16 GB bar.
LIVE_2233Z = [
    ("n201", {"mem": 56.0, "swap": 0.14, "psi": 0.0}, True),
    ("n202", {"mem": 40.0, "swap": 0.98, "psi": 0.41}, True),
    ("n105", {"mem": 11.6, "pressure": 1}, True),
    ("n101", {"mem": 7.3, "pressure": 2}, False),
]


@pytest.mark.parametrize(("name", "kw", "admitted"), LIVE_2233Z)
def test_the_2233z_readback_admits_h201_h202_h105_and_refuses_h101(
    name: str, kw: dict[str, Any], admitted: bool
) -> None:
    why = _lab(name=name, **kw).admission_refusal
    assert (why is None) is admitted, why
    if name == "n101":
        assert "mem_avail 7.3G < 10G" in why
        assert "macOS memory pressure warn (level 2) and free ~7.3G" in why


def test_a_claude_limited_host_takes_no_worker_until_its_marker_expires(
    tmp_path: Path,
) -> None:
    """On 2026-09-30 from ~21:47Z the n201 account answered every worker with "You've hit your weekly
    limit" and exit 1, and placement kept choosing n201 because its probe still read logged in with the
    most free slots (leases 514, 518-521). A per-host marker takes it out; the others still place."""
    from datetime import UTC, datetime, timedelta

    directory = tmp_path / "placement"
    now = datetime(2026, 9, 30, 21, 56, tzinfo=UTC)
    tail = "You've hit your weekly limit · resets Oct 3, 12pm (America/New_York)\n"
    assert (
        lp.limit_backoff_s(tail, now=now)
        == (datetime(2026, 10, 3, 16, 1, tzinfo=UTC) - now).total_seconds()
    )
    assert lp.limit_backoff_s("Claude usage limit reached.") == lp.LIMIT_BACKOFF_S
    assert (
        lp.limit_backoff_s("the gate hit a GitHub secondary rate limit; rerun queued")
        is None
    )
    until = lp.mark_limited(
        directory, "n201", lp.WEEKLY_LIMIT_BACKOFF_S, now=now, lease_id=521
    )
    assert until == "2026-10-01T03:56:00Z"
    lp.mark(directory, "n202", 7, os.getpid())
    assert lp.placed_counts(directory) == {"n202": 1}
    assert (directory / ".limited-n201").exists()
    limited = lp.limited_hosts(directory, now=now)
    assert limited == {"n201": until}

    def capture(argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, "PROBE 32 1.0 58720256", "")

    readings = lp.read_pool([HOSTS["n201"]], capture=capture, limited=limited)
    assert readings[0].error is None
    assert readings[0].host.engines == frozenset()
    assert lp.choose("claude_opus", readings) is None
    assert lp.read_pool([HOSTS["n201"]], capture=capture)[0].host.engines
    assert lp.limited_hosts(directory, now=now + timedelta(hours=7)) == {}
    assert not (directory / ".limited-n201").exists()


# ---------------------------------------------------------------------------------------------------
# OMN-17427: a host whose ~/.claude/settings.json does not set includeCoAuthoredBy to false takes no worker or lane.
# n202 lacked it on 2026-09-30 and 15 of the last 24 landing workers committed with a Claude co-author trailer.
# ---------------------------------------------------------------------------------------------------


def _coauthor(
    state: str | None, local: bool = False, name: str = "n202", mem: float = 40.0
) -> Any:
    return replace(_lab(mem, local=local, name=name), coauthor=state)


@pytest.mark.parametrize(
    ("text", "state"),
    [
        ("PROBE 32 1.0 50331648\nCOAUTHOR false\nLOGIN 1 1\n", "false"),
        ("PROBE 32 1.0 50331648\nCOAUTHOR unset\nLOGIN 1 1\n", "unset"),
        ("PROBE 32 1.0 50331648\nCOAUTHOR other\nLOGIN 1 1\n", "other"),
        ("PROBE 32 1.0 50331648\nCOAUTHOR nofile\nLOGIN 1 1\n", "nofile"),
        ("PROBE 32 1.0 50331648\nCOAUTHOR unreadable\nLOGIN 1 1\n", "unreadable"),
        ("PROBE 32 1.0 50331648\nLOGIN 1 1\n", "unreported"),
        ("PROBE 32 1.0 50331648\nCOAUTHOR false extra\nLOGIN 1 1\n", "unreported"),
    ],
)
def test_the_probe_coauthor_line_is_parsed_and_a_missing_line_is_unreported(
    text: str, state: str
) -> None:
    assert lp.coauthor_state(text) == state
    assert lp.parse_probe(_lab(0).host, text, 0).coauthor == state


def test_only_a_false_setting_passes_and_every_refusal_is_named() -> None:
    assert lp.coauthor_refusal("false") is None
    assert lp.coauthor_refusal(None) is None, (
        "a Reading built without a probe is not judged"
    )
    for state in ("unset", "other", "nofile", "unreadable", "unreported"):
        why = lp.coauthor_refusal(state) or ""
        assert why.startswith("COAUTHOR-TRAILER-NOT-DISABLED:")
        assert f"includeCoAuthoredBy is {state}" in why


def test_a_refused_host_holds_no_slot_however_idle_it_is_and_says_so() -> None:
    ok, bad = _coauthor("false"), _coauthor("unset")
    assert ok.slots > 0
    assert ok.admission_refusal is None
    assert ok.coauthor_refusal is None
    assert bad.slots == 0
    assert bad.cores == 32
    assert bad.load1 == 1.0
    assert (bad.admission_refusal or "").startswith("COAUTHOR-TRAILER-NOT-DISABLED")
    assert "ADMISSION-REFUSED(COAUTHOR-TRAILER-NOT-DISABLED" in bad.describe()
    assert "coauthor=unset" in bad.describe()
    assert "coauthor=false" in ok.describe()


def test_the_launching_host_is_judged_on_its_own_settings_too() -> None:
    assert _coauthor("false", local=True).admission_refusal is None
    assert _coauthor("false", local=True).slots > 0
    mac = _coauthor("unset", local=True, name="mac")
    assert mac.slots == 0
    assert "COAUTHOR-TRAILER-NOT-DISABLED" in (mac.admission_refusal or "")


def test_an_unreadable_host_reports_its_error_and_not_a_coauthor_refusal() -> None:
    reading_ = lp.Reading(_lab(0).host, error="ssh down", coauthor="unset")
    assert reading_.coauthor_refusal is None
    assert reading_.admission_refusal is None


def test_choose_skips_a_refused_host_and_none_is_left_when_all_are_refused() -> None:
    bad = _coauthor("unset", name="n202", mem=60.0)
    good = _coauthor("false", name="n201", mem=30.0)
    pick = lp.choose("claude_sonnet", [bad, good])
    assert pick is not None
    assert pick.host.name == "n201"
    assert lp.choose("claude_sonnet", [bad, _coauthor("nofile", name="n201")]) is None
    assert lp.pool_slots([bad]) == 0


@pytest.mark.parametrize(
    ("settings", "state"),
    [
        ('{"includeCoAuthoredBy": false}', "false"),
        ('{"includeCoAuthoredBy": true}', "other"),
        ('{"includeCoAuthoredBy": "false"}', "other"),
        ('{"model": "sonnet"}', "unset"),
        ("not json", "unreadable"),
        (None, "nofile"),
    ],
)
def test_the_real_probe_script_reads_the_settings_file_of_its_home(
    tmp_path: Path, settings: str | None, state: str
) -> None:
    (tmp_path / ".claude").mkdir()
    if settings is not None:
        (tmp_path / ".claude" / "settings.json").write_text(settings, encoding="utf-8")
    env = {**os.environ, "HOME": str(tmp_path)}
    done = subprocess.run(
        ["/bin/sh", "-c", lp._PROBE],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env=env,
    )
    assert done.returncode == 0, done.stderr
    assert lp.coauthor_state(done.stdout) == state, done.stdout
    names = [line.split()[0] for line in done.stdout.splitlines() if line.strip()]
    # Login is followed by the Codex capability lines. Coauthor settings must still be reported once,
    # before the login verdict; they do not depend on which capability the probe reports last.
    assert names.count("COAUTHOR") == names.count("LOGIN") == 1
    assert names.index("COAUTHOR") < names.index("LOGIN")


# Lane lane-placement (OMN-17427), 2026-10-01: n202 ran 332 of the day's 365 remote lanes, n201 33, the Macs none, and
# n105's link lost 40% of 20 pings from n202 (40-80% from n200 at 04:05Z, FRICTION 2026-10-01T04:34:32Z).
LINUX_PING = (
    "--- 192.0.2.35 ping statistics ---\n"
    "20 packets transmitted, 12 received, 40% packet loss, time 3861ms\n"
    "rtt min/avg/max/mdev = 8.073/39.560/123.130/36.315 ms\n"
)
MAC_PING = (
    "--- 192.0.2.41 ping statistics ---\n"
    "10 packets transmitted, 10 packets received, 0.0% packet loss\n"
    "round-trip min/avg/max/stddev = 1.1/1.4/2.0/0.2 ms\n"
)


def _host(name: str, target: str) -> Any:
    return lp.Host(
        name=name,
        target=target,
        engines=frozenset(),
        cap=None,
        gh=False,
        engines_live=True,
    )


def test_link_loss_is_read_from_the_linux_and_the_macos_ping_summary() -> None:
    assert lp.parse_link_loss(LINUX_PING) == 40.0
    assert lp.parse_link_loss(MAC_PING) == 0.0
    assert (
        lp.parse_link_loss(
            "10 packets transmitted, 0 received, +10 errors, 100% packet loss, time 9ms"
        )
        == 100.0
    )
    assert lp.parse_link_loss("ping: unknown host") is None
    assert lp.parse_link_loss("") is None


def test_the_ping_is_a_plain_unprivileged_count_to_the_rows_address() -> None:
    argv = lp.ping_argv("jonah@192.0.2.35")
    assert argv[0] == "ping"
    assert argv[-1] == "192.0.2.35", "the ssh user is not part of the address"
    assert argv[argv.index("-c") + 1] == str(lp.LINK_PING_COUNT)
    assert float(argv[argv.index("-i") + 1]) >= 0.2


def test_a_lab_host_losing_over_the_bar_in_two_consecutive_reads_is_refused_and_says_why() -> (
    None
):
    """The bars and their fixtures are in tests/test_landing_link_loss.py (OMN-17427): one 10-ping sample at 10% refused n203 on
    2026-10-18, so the gate now judges 50 pings and benches only on sustained loss over 20%."""
    assert lp.ADMIT_MAX_LINK_LOSS_PCT == 20.0
    lossy = replace(
        _lab(40.0, name="n105", pressure=1), link_loss_pct=40.0, link_loss_prior=(40.0,)
    )
    why = lossy.admission_refusal or ""
    assert "LINK-LOSS 40% of pings lost, over 20% in 2 consecutive reads" in why
    assert lossy.slots == 0
    assert "loss=40%" in lossy.describe()
    assert "ADMISSION-REFUSED(LINK-LOSS" in lossy.describe()
    for ok in (None, 0.0, 5.0, 40.0):
        assert replace(_lab(40.0), link_loss_pct=ok).admission_refusal is None, ok
    assert (
        replace(_lab(40.0, local=True), link_loss_pct=90.0).admission_refusal is None
    ), "the launching host is not pinged"


def test_the_local_probe_outlasts_the_launching_hosts_background_throttle() -> None:
    """OMN-19998: launchd runs the controller as ``ProcessType Background``, and the probe it forks on its own host
    inherits that throttle. On n200 at load 12 the same probe took 7 s in the foreground and 25 s under
    ``taskpolicy -b`` (2026-10-15T19:30Z), so with the 20 s ssh timeout n200 read ``UNREADABLE(rc=255 timed out after
    20s)`` in 189 of 200 placements and its cap of 12 left the pool. The local probe has its own longer bound; a remote
    probe keeps the ssh bound; a ping has its own, which outlasts the packets."""
    assert lp.LOCAL_PROBE_TIMEOUT_S >= 2 * lp.PROBE_TIMEOUT_S
    timeouts: dict[str, float] = {}

    def capture(argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
        kind = (
            "ping"
            if argv[0] == "ping"
            else "local"
            if argv[0] == "/bin/sh"
            else "remote"
        )
        timeouts.setdefault(kind, timeout)
        if kind == "local" and timeout < 25:
            return subprocess.CompletedProcess(
                argv, 255, "", f"timed out after {timeout:.0f}s"
            )
        return subprocess.CompletedProcess(
            argv, 0, "PROBE 24 12.0 58720256\nCOAUTHOR false\nLOGIN 1 1\n", ""
        )

    readings = {
        r.host.name: r
        for r in lp.read_pool([HOSTS["mac"], HOSTS["n201"]], capture=capture)
    }
    assert timeouts == {
        "local": lp.LOCAL_PROBE_TIMEOUT_S,
        "remote": lp.PROBE_TIMEOUT_S,
        "ping": lp.link_gate.LINK_PING_TIMEOUT_S,
    }
    assert readings["mac"].error is None
    assert readings["mac"].cores == 24
    assert readings["n201"].error is None


def test_read_pool_pings_every_remote_host_and_never_the_local_one() -> None:
    hosts = [
        lp.Host(
            name="n200",
            target=None,
            engines=frozenset(),
            cap=None,
            gh=False,
            engines_live=True,
        ),
        _host("n105", "192.0.2.35"),
        _host("n201", "192.0.2.41"),
    ]
    pings: list[str] = []

    def capture(argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
        if argv[0] == "ping":
            pings.append(argv[-1])
            return subprocess.CompletedProcess(
                argv,
                1 if argv[-1].endswith(".35") else 0,
                LINUX_PING if argv[-1].endswith(".35") else MAC_PING,
                "",
            )
        return subprocess.CompletedProcess(
            argv, 0, "PROBE 32 1.0 58720256\nCOAUTHOR false\nLOGIN 1 1\n", ""
        )

    readings = {r.host.name: r for r in lp.read_pool(hosts, capture=capture)}
    assert sorted(pings) == ["192.0.2.35", "192.0.2.41"]
    assert readings["n200"].link_loss_pct is None
    assert readings["n105"].link_loss_pct == 40.0
    assert readings["n105"].link_verdict.degraded
    assert readings["n105"].admission_refusal is None, "one read is not sustained loss"
    assert "LINK-LOSS" in (
        replace(readings["n105"], link_loss_prior=(40.0,)).admission_refusal or ""
    )
    assert readings["n201"].link_loss_pct == 0.0
    assert readings["n201"].admission_refusal is None
    # A ping that printed no summary (no ping binary, ICMP filtered and timed out) leaves the host unjudged by loss.
    quiet = lp.read_pool(
        [hosts[1]],
        capture=lambda argv, **_: subprocess.CompletedProcess(
            argv,
            0 if argv[0] != "ping" else 2,
            "" if argv[0] == "ping" else "PROBE 32 1.0 58720256\nLOGIN 1 1\n",
            "",
        ),
    )
    assert quiet[0].link_loss_pct is None


def test_the_mac_probe_reports_its_lan_grant_and_the_linux_probe_does_not() -> None:
    linux_branch, mac_branch = (
        lp._PROBE.split("else", 1)[0],
        lp._PROBE.split("else", 1)[1].split("\nfi\n", 1)[0],
    )
    assert "LANGRANT" not in linux_branch
    assert "SSH_CLIENT" in mac_branch
    assert "LANGRANT" in lp._PROBE
    for state in ("ok", "denied", "unknown"):
        text = f"PROBE 10 1.0 50331648\nPRESSURE 1\nLANGRANT {state}\nCOAUTHOR false\nLOGIN 1 1\n"
        assert lp.parse_probe(_host("n105", "192.0.2.35"), text, 0).lan_grant == state
    assert (
        lp.parse_probe(
            _host("n202", "192.0.2.42"), "PROBE 32 1.0 58720256\nLOGIN 1 1\n", 0
        ).lan_grant
        is None
    )


def test_a_mac_qualifies_only_while_its_lan_grant_and_its_login_hold() -> None:
    def mac(grant: str, login: str) -> Any:
        text = f"PROBE 10 1.0 50331648\nPRESSURE 1\nLANGRANT {grant}\nCOAUTHOR false\nLOGIN {login} 1\n"
        return lp.parse_probe(_host("n105", "192.0.2.35"), text, 0)

    held = mac("ok", "1")
    assert held.admission_refusal is None
    assert held.slots > 0
    assert lp.choose("claude_sonnet", [held]) is not None
    for grant in ("denied", "unknown"):
        why = mac(grant, "1").admission_refusal or ""
        assert f"MAC-LAN-GRANT-MISSING: Local Network access {grant}" in why
        assert lp.choose("claude_sonnet", [mac(grant, "1")]) is None
    assert lp.choose("claude_sonnet", [mac("ok", "0")]) is None, (
        "no Claude login, no lane"
    )


def test_the_pick_ranks_every_eligible_host_of_the_shared_table_by_spare_capacity(
    tmp_path: Path,
) -> None:
    """The table as the private data clone ships it: n200 (the launching Mac Studio, local), n105, n101, n202, n201. With
    the PCs busy and n105's link clean, a Mac takes the lane; when n105's link is lossy the next spare host does (a lossy
    link ranks after clean ones, and benches only on sustained loss: tests/test_landing_link_loss.py)."""
    table = tmp_path / "lab_run_hosts.yaml"
    table.write_text(
        "hosts:\n  - name: n200\n    target: local\n    local: true\n"
        "  - name: n105\n    target: 192.0.2.35\n  - name: n101\n    target: 192.0.2.31\n"
        "  - name: n202\n    target: 192.0.2.42\n    model_port: 8000\n    penalty: 0.1\n"
        "  - name: n201\n    target: 192.0.2.41\n    model_port: 8000\n    penalty: 0.25\n",
        encoding="utf-8",
    )
    pool = lp.load_pool({"ONEX_LAB_RUN_HOSTS": str(table)})
    assert [h.name for h in pool] == ["n200", "n105", "n101", "n202", "n201"]
    probe = {
        "n200": "PROBE 24 30.0 33554432\nPRESSURE 1\nLANGRANT unknown",
        "n105": "PROBE 10 0.5 25165824\nPRESSURE 1\nLANGRANT ok",
        "n101": "PROBE 12 1.0 7340032\nPRESSURE 2\nLANGRANT ok",
        "n202": "PROBE 32 29.0 41943040\nBUSY 29.0",
        "n201": "PROBE 32 30.0 62914560\nBUSY 30.0",
    }

    def read(loss_105: str) -> list[Any]:
        def capture(
            argv: list[str], *, timeout: float
        ) -> subprocess.CompletedProcess[str]:
            if argv[0] == "ping":
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    LINUX_PING if argv[-1].endswith(".35") and loss_105 else MAC_PING,
                    "",
                )
            name = (
                "n200"
                if argv[0] == "/bin/sh"
                else next(h.name for h in pool if h.target == argv[-2])
            )
            return subprocess.CompletedProcess(
                argv, 0, probe[name] + "\nCOAUTHOR false\nLOGIN 1 1\n", ""
            )

        return lp.read_pool(pool, capture=capture)

    lab = [r for r in read("") if not r.host.local and r.admission_refusal is None]
    pick = lp.choose("claude_sonnet", lab)
    assert pick is not None, [r.describe() for r in lab]
    assert pick.host.name == "n105", [r.describe() for r in lab]
    assert {r.host.name for r in lab} == {"n105", "n202", "n201"}, (
        "n101 is refused on memory and pressure"
    )
    lab = [r for r in read("lossy") if not r.host.local and r.admission_refusal is None]
    pick = lp.choose("claude_sonnet", lab)
    assert pick is not None
    assert pick.host.name == "n202"
    assert {r.host.name for r in lab} == {"n105", "n202", "n201"}, (
        "a lossy link still admits on one read"
    )
    lab = [
        replace(r, link_loss_prior=(40.0,)) if r.host.name == "n105" else r
        for r in read("lossy")
    ]
    assert "n105" not in {
        r.host.name for r in lab if not r.host.local and r.admission_refusal is None
    }


# OMN-20367: a host at its CPU or DIMM temperature limit takes no new placement. On 2026-10-02 n202 reached the board's
# 95C readout (Ryzen Tjmax) at about 20% CPU use, with the DIMM sensors' 55C alarm latched once.
def _hot(
    cpu: float | None, dimm: float | None, *, local: bool = False, mem: float = 40.0
) -> Any:
    return replace(_lab(mem, local=local), cpu_temp_c=cpu, dimm_temp_c=dimm)


def test_over_cpu_bar_takes_zero_slots() -> None:
    assert lp.ADMIT_MAX_CPU_C == 85.0
    hot = _hot(91.2, 40.0)
    assert hot.admission_refusal == "cpu 91.2C > 85C"
    assert hot.slots == 0
    assert hot.idle
    assert "ADMISSION-REFUSED(cpu 91.2C > 85C)" in hot.describe()
    assert _hot(85.0, 40.0).admission_refusal is None, "the bar is strictly over 85C"
    assert _hot(85.1, None).slots == 0
    # The controller host is judged on temperature too, and keeps no slot while hot.
    assert _hot(90.0, None, local=True).slots == 0
    assert "cpu 90.0C > 85C" in (_hot(90.0, None, local=True).admission_refusal or "")


def test_over_dimm_bar_takes_zero_slots() -> None:
    assert lp.ADMIT_MAX_DIMM_C == 70.0
    hot = _hot(60.0, 72.5)
    assert hot.admission_refusal == "dimm 72.5C > 70C"
    assert hot.slots == 0
    assert "ADMISSION-REFUSED(dimm 72.5C > 70C)" in hot.describe()
    assert _hot(60.0, 70.0).admission_refusal is None, "the bar is strictly over 70C"
    both = _hot(88.0, 75.0).admission_refusal or ""
    assert "cpu 88.0C > 85C" in both
    assert "dimm 75.0C > 70C" in both
    # The sibling bars still say their piece beside the thermal one.
    assert "mem_avail 6.0G < 10G" in (_hot(90.0, None, mem=6.0).admission_refusal or "")


def test_under_both_bars_and_no_sensors_unchanged() -> None:
    cool = _hot(83.4, 39.3)
    assert cool.admission_refusal is None
    assert cool.slots > 0
    assert ",cpu=83.4C,dimm=39.3C," in cool.describe()
    blind = _hot(None, None)
    assert blind.admission_refusal is None
    assert blind.slots == cool.slots
    assert "cpu=" not in blind.describe()
    assert "dimm=" not in blind.describe()
    assert replace(blind, cpu_temp_c=70.0).describe().count("dimm=") == 0, (
        "only a sensor that was read is reported"
    )
    # Every existing bar still refuses on its own.
    assert _hot(40.0, 40.0, mem=6.0).slots == 0
    # The probe parser: sensors, one sensor, none, a Mac, a garbled line.
    base = "PROBE 32 1.0 50331648\nCOAUTHOR false\nLOGIN 1 1\n"
    host = _lab(0).host
    both = lp.parse_probe(host, "THERMAL 83.4 39.3\n" + base, 0)
    assert (both.cpu_temp_c, both.dimm_temp_c) == (83.4, 39.3)
    cpu_only = lp.parse_probe(host, "THERMAL 70.5 -\n" + base, 0)
    assert (cpu_only.cpu_temp_c, cpu_only.dimm_temp_c) == (70.5, None)
    none = lp.parse_probe(host, base, 0)
    assert (none.cpu_temp_c, none.dimm_temp_c) == (None, None)
    assert lp.parse_thermal("THERMAL x 40.0\n") == (None, 40.0)
    assert lp.parse_thermal("THERMAL 1 2 3\n") == (None, None)
    mac_branch = lp._PROBE.split("else", 1)[1].split("fi", 1)[0]
    assert "hwmon" not in mac_branch
    assert "THERMAL" not in mac_branch


def _probe_fn(name: str) -> str:
    """The probe's shell text from ``name() {`` to its closing line, or the one command that sets ``name=$(``."""
    text = lp._PROBE
    start = text.index(f"  {name}() {{" if name == "tm" else f"  {name}=$(")
    return text[
        start : text.index("\n  n=$(nproc)" if name == "tm" else "}')\n", start)
        + (0 if name == "tm" else 4)
    ]


def test_probe_reads_k10temp_tctl_coretemp_package_and_the_hottest_dimm(
    tmp_path: Path,
) -> None:
    def sensor(hw: str, name: str, **files: str) -> None:
        d = tmp_path / hw
        d.mkdir()
        (d / "name").write_text(name + "\n")
        for fname, value in files.items():
            (d / fname).write_text(value + "\n")

    sensor("hwmon0", "nvme", temp1_input="31000", temp1_label="Composite")
    sensor(
        "hwmon4",
        "k10temp",
        temp1_input="83375",
        temp1_label="Tctl",
        temp3_input="68625",
        temp3_label="Tccd1",
    )
    sensor("hwmon5", "spd5118", temp1_input="36500")
    sensor("hwmon6", "spd5118", temp1_input="39250")
    script = _probe_fn("tm").replace("/sys/class/hwmon", str(tmp_path)) + "\ntm\n"
    done = subprocess.run(
        ["/bin/sh", "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert done.stdout.split() == ["83375", "39250"], done.stderr
    # Intel: the Package id label; no DIMM sensor.
    for d in tmp_path.iterdir():
        for f in d.iterdir():
            f.unlink()
        d.rmdir()
    sensor(
        "hwmon1",
        "coretemp",
        temp1_input="45000",
        temp1_label="Package id 0",
        temp2_input="40000",
        temp2_label="Core 0",
    )
    done = subprocess.run(
        ["/bin/sh", "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert done.stdout.split() == ["45000", "-"], done.stderr
    # No sensors at all.
    (tmp_path / "hwmon1" / "name").write_text("nct6686\n")
    done = subprocess.run(
        ["/bin/sh", "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert done.stdout.split() == ["-", "-"], done.stderr


def test_probe_averages_two_reads() -> None:
    text = lp._PROBE
    first, second = text.index("w1=$(tm)"), text.index("w2=$(tm)")
    sample = text.index("sleep 1")
    assert first < sample < second, (
        "one read before the one-second CPU sample, one after it"
    )
    averaging = _probe_fn("th")

    def averaged(w1: str, w2: str) -> str:
        script = f'w1="{w1}"; w2="{w2}"\n{averaging}\necho "$th"'
        done = subprocess.run(
            ["/bin/sh", "-c", script],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        return done.stdout.strip()

    assert averaged("90000 40000", "80000 42000") == "85.0 41.0", (
        "a one-read spike of 90C averages with 80C"
    )
    assert averaged("83375 36500", "83375 36500") == "83.4 36.5"
    assert averaged("90000 -", "-  -") == "90.0 -", (
        "a read that failed does not count as zero"
    )
    assert averaged("- -", "- -") == "", "no sensors prints no THERMAL line"
    assert 'echo "THERMAL $th"' in text


# --- OMN-17427: a stated reset time becomes the marker's until --------------------------------------------
# On 2026-10-03 six lanes ended on "You've hit your weekly limit · resets Oct 5, 4am (America/New_York)" and
# the marker was flat six hours (until 15:43Z), so the host went back into placement ~44 hours before the
# account could answer, and each lane placed there failed at its first request.


def _limit_now():
    from datetime import UTC, datetime

    return datetime(2026, 10, 3, 9, 43, tzinfo=UTC)


@pytest.mark.parametrize(
    "text",
    [
        "You've hit your weekly limit · resets Oct 5, 4am (America/New_York)",
        "You've hit your weekly limit · resets Oct 5 at 4am",
        "You've hit your weekly limit · resets Oct 5, 4:00 AM (America/New_York)",
    ],
)
def test_a_dated_weekly_reset_is_the_absolute_until_in_the_stated_zone(
    text: str, tmp_path: Path
) -> None:
    from datetime import UTC, datetime

    now = _limit_now()
    reset = datetime(2026, 10, 5, 8, 0, tzinfo=UTC)  # 4am EDT (UTC-4)
    seconds = lp.limit_backoff_s(text, now=now)
    assert seconds == (reset - now).total_seconds() + lp.RESET_MARGIN_S
    assert lp.mark_limited(tmp_path, "n201", seconds, now=now) == "2026-10-05T08:01:00Z"


def test_a_zone_in_the_text_is_honoured_and_a_missing_zone_is_the_operator_zone() -> (
    None
):
    now = _limit_now()
    utc = lp.limit_backoff_s(
        "You've hit your weekly limit · resets Oct 5, 4am (UTC)", now=now
    )
    ny = lp.limit_backoff_s(
        "You've hit your weekly limit · resets Oct 5, 4am (America/New_York)", now=now
    )
    assert ny - utc == 4 * 3600
    assert (
        lp.limit_backoff_s(
            "You've hit your weekly limit · resets Oct 5 at 4am", now=now
        )
        == ny
    )


def test_the_five_hour_form_resets_at_the_next_such_time_of_day() -> None:
    from datetime import UTC, datetime

    now = datetime(2026, 10, 1, 16, 0, tzinfo=UTC)  # 12:00 EDT
    text = "You've hit your limit · resets 1:10pm (America/New_York)"
    assert lp.limit_backoff_s(text, now=now) == 70 * 60 + lp.RESET_MARGIN_S
    # a time already past today is tomorrow's
    late = datetime(2026, 10, 1, 18, 0, tzinfo=UTC)  # 14:00 EDT
    assert lp.limit_backoff_s(text, now=late) == (23 * 60 + 10) * 60 + lp.RESET_MARGIN_S


def test_a_dated_reset_already_past_this_year_is_not_guessed_into_next_year() -> None:
    now = _limit_now()
    text = "You've hit your weekly limit · resets Oct 1, 4am (America/New_York)"
    assert lp.limit_backoff_s(text, now=now) == lp.WEEKLY_LIMIT_BACKOFF_S


@pytest.mark.parametrize(
    ("text", "default"),
    [
        (
            "You've hit your weekly limit · resets sometime soon",
            "WEEKLY_LIMIT_BACKOFF_S",
        ),
        ("You've hit your weekly limit", "WEEKLY_LIMIT_BACKOFF_S"),
        ("You've hit your limit · resets whenever", "LIMIT_BACKOFF_S"),
        ("Claude usage limit reached.", "LIMIT_BACKOFF_S"),
        ("You've hit your weekly limit · resets Foo 5, 4am", "WEEKLY_LIMIT_BACKOFF_S"),
        ("You've hit your weekly limit · resets Oct 5, 4am (Not/AZone)", None),
    ],
)
def test_an_unreadable_reset_still_marks_the_host_for_the_safe_default(
    text: str, default: str | None
) -> None:
    seconds = lp.limit_backoff_s(text, now=_limit_now())
    assert seconds is not None  # never left unmarked
    if default is not None:
        assert seconds == getattr(lp, default)


def test_a_reset_beyond_a_week_and_a_day_is_not_trusted() -> None:
    text = "You've hit your weekly limit · resets Oct 20, 4am (America/New_York)"
    assert lp.limit_backoff_s(text, now=_limit_now()) == lp.WEEKLY_LIMIT_BACKOFF_S


# --- OMN-20498: a host whose Claude OAuth session expired ---------------------------------------------------

H202_AUTH_TEXT = (
    "Failed to authenticate: OAuth session expired and could not be refreshed"
)


@pytest.mark.parametrize(
    "text",
    [
        H202_AUTH_TEXT,
        "Failed to authenticate",
        'API Error: 401 {"type":"error","error":{"type":"authentication_error","message":"Invalid bearer token"}}',
        "Please run /login · API Error: 401 authentication failed",
    ],
)
def test_auth_failure_text_is_recognised(text: str) -> None:
    assert lp.auth_failure(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "two open PRs, merged",
        "You've hit your weekly limit · resets Oct 3, 12pm (America/New_York)",
        "the gate hit a GitHub secondary rate limit; rerun queued",
        "HTTP 500 internal error from the proxy",
        "",
    ],
)
def test_unrelated_text_is_not_an_auth_failure(text: str) -> None:
    assert lp.auth_failure(text) is False


def test_auth_mark_is_its_own_file_with_kind_and_engine_and_leaves_limited_hosts_alone(
    tmp_path: Path,
) -> None:
    from datetime import UTC, datetime, timedelta

    directory = tmp_path / "placement"
    now = datetime(2026, 10, 4, 3, 0, tzinfo=UTC)
    until = lp.mark_auth_expired(directory, "n202", now=now, lease_id=7)
    assert until == "2026-10-11T03:00:00Z"
    assert lp.AUTH_EXPIRY_S == 7 * 86400
    marker = directory / ".auth-expired-claude-n202"
    body = json.loads(marker.read_text())
    assert body == {
        "at": "2026-10-04T03:00:00Z",
        "engine": "claude",
        "host": "n202",
        "kind": "auth-expired",
        "lease_id": 7,
        "until": until,
    }
    assert lp.limited_hosts(directory, now=now) == {}  # existing callers never see it
    assert lp.auth_expired_hosts(directory, now=now) == {"n202": "2026-10-04T03:00:00Z"}
    assert lp.auth_expired_hosts(directory, engine="codex", now=now) == {}
    # a usage-limit mark and an auth mark for one host do not clobber each other
    lim = lp.mark_limited(directory, "n202", 3600.0, now=now)
    assert lp.limited_hosts(directory, now=now) == {"n202": lim}
    assert lp.auth_expired_hosts(directory, now=now) == {"n202": "2026-10-04T03:00:00Z"}
    # lasts until cleared, bounded by the safety expiry
    assert "n202" in lp.auth_expired_hosts(directory, now=now + timedelta(days=6))
    assert lp.auth_expired_hosts(directory, now=now + timedelta(days=8)) == {}
    assert not marker.exists()
    # the operator clear is deleting the file
    lp.mark_auth_expired(directory, "n202", now=now)
    marker.unlink()
    assert lp.auth_expired_hosts(directory, now=now) == {}


def test_an_auth_expired_host_holds_no_claude_engine_in_the_pool_read(
    tmp_path: Path,
) -> None:
    def capture(argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, "PROBE 32 1.0 58720256", "")

    readings = lp.read_pool(
        [HOSTS["n201"]], capture=capture, auth_expired={"n201": "2026-10-04T03:00:00Z"}
    )
    assert readings[0].error is None
    assert readings[0].host.engines == frozenset()
    assert lp.read_pool([HOSTS["n201"]], capture=capture, auth_expired={"n202": "x"})[
        0
    ].host.engines


def test_claude_lane_preflight_uses_the_local_lane_admission_bar() -> None:
    # A local worker can have slots below the lane's memory floor; choose_lane refuses it.
    r = reading("mac", 24, 3.0, memory=8.0)
    assert r.slots > 0
    assert r.lane_slots == 0
    assert lp.choose_lane("claude_sonnet", [r]) is None
    assert lp.preflight_skips([r], "claude_sonnet", lane=False) == {}
    assert (
        "mem_avail 8.0G < 10G"
        in lp.preflight_skips([r], "claude_sonnet", lane=True)["mac"]
    )
