# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A Mac is admitted on measured free memory, not on the kernel's 'warn' pressure level alone (OMN-19998).

On 2026-10-17 ~10:58Z the Mac Studio (n200, 192 GB, 24 cores) read kern.memorystatus_vm_pressure_level=2 (warn) with 32% of
memory free (~61 GB) and no lab lane running (the Docker VM, the local model server and fseventsd held the rest), and placement
refused every Claude and Codex unit on it all night with 'macOS memory pressure level 2 >= 2'. The bar is now: refuse at level 4
(critical) and under a free-percentage floor, admit at warn while the free memory covers a lane's declared need plus a margin,
and print the measured free figure in every refusal. Offline: the readings are built from the recorded numbers."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_landing_placement_effect.handlers import (
    handler_landing_placement as lp,
)

# n200 as the shared table declares it: the launching host, 24 cores, a cap of 12 and 1.5 cores a unit.
N200 = lp.load_pool(
    {
        "LANDING_POOL_HOSTS": json.dumps(
            [
                {
                    "name": "n200",
                    "local": True,
                    "engines": ["claude_sonnet", "claude_opus"],
                    "cap": 12,
                    "unit_cores": 1.5,
                },
            ]
        )
    }
)[0]
TOTAL_GB = 192.0

# The 2026-10-17T10:58Z reading: warn, 32% free of 192 GB, nothing running.
TONIGHT = {
    "cores": 24,
    "load1": 2.0,
    "cpu_idle_pct": 90.0,
    "busy_cores": 2.4,
    "mem_avail_gb": 0.32 * TOTAL_GB,
    "mem_pressure": 2,
    "mem_free_pct": 32.0,
    "mem_total_gb": TOTAL_GB,
}


def mac(**over: Any) -> Any:
    return lp.Reading(host=N200, **(TONIGHT | over))


def test_tonights_warn_with_61_gb_free_is_admitted_for_lanes() -> None:
    n200 = mac()
    assert n200.mem_pressure == 2
    assert n200.admission_refusal is None
    assert n200.lane_admission_refusal is None
    assert n200.lane_slots > 0
    assert "ADMISSION-REFUSED" not in n200.describe()
    assert "LANE-REFUSED" not in n200.describe()


def test_tonights_slots_follow_the_per_unit_cost_model_unchanged() -> None:
    # 24 cores at the launching host's 0.75 per core less load 2.0 is 16 free cores, a unit costs 1.5: 10. Memory allows
    # (61.4 - 4) // 2 = 28 and the row's cap is 12, so cores bind. The warn level takes none of them away.
    assert mac().lane_slots == mac(mem_pressure=1).lane_slots == 10


def test_critical_pressure_refuses_whatever_memory_is_free_and_prints_the_figure() -> (
    None
):
    critical = mac(mem_pressure=4)
    why = critical.lane_admission_refusal or ""
    assert "critical" in why
    assert "level 4" in why
    assert "free 32% (~61.4G of 192G)" in why
    assert critical.lane_slots == 0
    assert "LANE-REFUSED(" in critical.describe()
    assert "free 32%" in critical.describe()


def test_a_free_percentage_under_the_floor_refuses_even_at_normal_pressure() -> None:
    low = mac(mem_pressure=1, mem_free_pct=4.0, mem_avail_gb=0.04 * TOTAL_GB)
    why = low.lane_admission_refusal or ""
    assert "free 4% (~7.7G of 192G)" in why
    assert f"{lp.ADMIT_MAC_MIN_FREE_PCT:.0f}%" in why
    assert low.lane_slots == 0


def test_warn_with_free_memory_under_a_lane_plus_margin_is_refused_with_the_figure() -> (
    None
):
    # A 64 GB Mac with 12 GB free (19%) clears the percentage floor and the 10 GB floor, but not a lane's 8 GB plus the margin.
    tight = mac(mem_total_gb=64.0, mem_free_pct=12 / 64 * 100, mem_avail_gb=12.0)
    why = tight.lane_admission_refusal or ""
    assert "warn" in why
    assert "free 19% (~12.0G of 64G)" in why
    assert "< 16G" in why
    assert tight.lane_slots == 0


def test_the_warn_bar_is_the_lane_need_plus_the_margin() -> None:
    need = lp.ADMIT_LANE_MEM_MAX_GB + lp.ADMIT_MAC_WARN_MARGIN_GB
    assert need == 16.0

    def small_mac(free_gb: float) -> Any:
        return mac(
            mem_total_gb=64.0, mem_free_pct=free_gb / 64 * 100, mem_avail_gb=free_gb
        )

    assert small_mac(need).lane_admission_refusal is None
    assert small_mac(need - 0.5).lane_admission_refusal is not None


def test_on_192_gb_the_percentage_floor_binds_before_the_warn_bar() -> None:
    # 10% of 192 GB is 19.2 GB, over a lane plus margin (16 GB): 18 GB free is refused by the floor, 20 GB is admitted at warn.
    floor = mac(mem_free_pct=18 / TOTAL_GB * 100, mem_avail_gb=18.0)
    assert "floor" in (floor.lane_admission_refusal or "")
    assert (
        mac(mem_free_pct=20 / TOTAL_GB * 100, mem_avail_gb=20.0).lane_admission_refusal
        is None
    )


def test_a_warn_mac_with_no_kernel_free_percentage_is_judged_on_the_available_figure() -> (
    None
):
    admitted = mac(mem_free_pct=None, mem_total_gb=None, mem_avail_gb=40.0)
    assert admitted.lane_admission_refusal is None
    refused = mac(mem_free_pct=None, mem_total_gb=None, mem_avail_gb=12.0)
    why = refused.lane_admission_refusal or ""
    assert "warn" in why
    assert "free ~12.0G" in why


def test_memory_under_the_ten_gb_floor_still_refuses_alone() -> None:
    assert "mem_avail 9.0G < 10G" in (
        mac(mem_pressure=1, mem_free_pct=None, mem_avail_gb=9.0).lane_admission_refusal
        or ""
    )


def test_the_constants_are_declared_as_data() -> None:
    assert lp.ADMIT_MAC_CRITICAL_PRESSURE == 4
    assert lp.ADMIT_MAC_WARN_PRESSURE == 2
    assert lp.ADMIT_MAC_MIN_FREE_PCT == 10.0
    assert lp.ADMIT_MAC_WARN_MARGIN_GB == 8.0
    assert not hasattr(lp, "ADMIT_MAC_MAX_PRESSURE"), "the level-2 refusal is gone"


def test_the_probe_reads_the_kernel_free_percentage_and_the_total_on_macos_only() -> (
    None
):
    mac_branch = lp._PROBE.split("else\n", 1)[1]
    assert "kern.memorystatus_level" in mac_branch
    assert "hw.memsize" in mac_branch
    assert 'echo "MEMFREE $mf $mt"' in lp._PROBE
    assert "kern.memorystatus_level" not in lp._PROBE.split("else\n", 1)[0]


@pytest.mark.parametrize(
    ("level", "memsize", "want"),
    [
        ("32", "206158430208", "MEMFREE 32 206158430208"),
        ("", "206158430208", "none"),
        ("32", "", "none"),
        ("n/a", "206158430208", "none"),
    ],
)
def test_the_probe_snippet_prints_a_memfree_line_only_when_both_numbers_read(
    tmp_path: Path, level: str, memsize: str, want: str
) -> None:
    fake = tmp_path / "sysctl"
    fake.write_text(
        '#!/bin/sh\ncase "$2" in\n'
        f'  kern.memorystatus_level) echo "{level}";;\n  hw.memsize) echo "{memsize}";;\nesac\n',
        encoding="utf-8",
    )
    fake.chmod(0o755)
    script = (
        lp.MAC_MEM_FREE_SH
        + '\n[ -n "$mf" ] && [ -n "$mt" ] && echo "MEMFREE $mf $mt"\necho "end"\n'
    )
    out = subprocess.run(
        ["/bin/sh", "-c", script],
        capture_output=True,
        text=True,
        check=True,
        env={"PATH": f"{tmp_path}:/usr/bin:/bin"}
        | {k: v for k, v in os.environ.items() if k == "HOME"},
    ).stdout
    lines = out.strip().splitlines()
    assert (lines[0] if want != "none" else "none") == want
    assert lines[-1] == "end"


def test_parse_probe_reads_the_memfree_line_into_the_reading() -> None:
    text = (
        "PROBE 24 2.0 64424509\nCPUIDLE 90.0\nPRESSURE 2\nMEMFREE 32 206158430208\n"
        "COAUTHOR false\nLOGIN 1 1\n"
    )
    parsed = lp.parse_probe(N200, text, 0)
    assert parsed.mem_free_pct == 32.0
    assert parsed.mem_total_gb == pytest.approx(192.0)
    assert parsed.mem_pressure == 2
    assert parsed.lane_admission_refusal is None
    absent = lp.parse_probe(
        N200, "PROBE 24 2.0 64424509\nPRESSURE 2\nCOAUTHOR false\nLOGIN 1 1\n", 0
    )
    assert absent.mem_free_pct is None
    assert absent.mem_total_gb is None


def test_describe_round_trips_the_free_figure() -> None:
    line = mac().describe()
    assert "memfree=32%" in line
    assert "memtotal=192.0G" in line
    back = lp.Reading.from_description(N200, line)
    assert back is not None
    assert back.mem_free_pct == 32.0
    assert back.mem_total_gb == 192.0
    assert back.mem_pressure == 2
    assert back.lane_admission_refusal is None
    refused = mac(mem_pressure=4).describe()
    again = lp.Reading.from_description(N200, refused)
    assert again is not None
    assert again.lane_admission_refusal is not None
