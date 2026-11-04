# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A macOS lab host is admitted on measured CPU idle, with load1 kept as a secondary guard at a higher bar (OMN-20434).

macOS load1 counts threads blocked on IO, so it overstates CPU demand: n101 (12 cores) on 2026-10-03 ~22:38 local read
load 43.06/37.82/45.69 with `top -l 2` at 31.7% user, 9.6% sys, 58.7% idle, and was refused as LOAD-OVER-BAR. Offline:
the probe's top parsing runs against a recorded `top` on PATH, and readings are built from the recorded numbers."""

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

# `top -l 2 -n 0 -s 1` prints two samples; the first is the average since boot, the second is the live one.
H101_TOP = """\
Processes: 612 total, 9 running, 603 sleeping, 3011 threads
Load Avg: 24.70, 37.82, 45.69  CPU usage: 3.10% user, 2.20% sys, 94.70% idle
PhysMem: 15G used (2G wired), 1G unused.

Processes: 612 total, 11 running, 601 sleeping, 3040 threads
Load Avg: 43.06, 37.82, 45.69  CPU usage: 31.7% user, 9.6% sys, 58.7% idle
PhysMem: 15G used (2G wired), 1G unused.
"""
SATURATED_TOP = H101_TOP.replace(
    "31.7% user, 9.6% sys, 58.7% idle", "71.2% user, 21.4% sys, 7.4% idle"
)


def lab(name: str = "n101", **over: Any) -> Any:
    host = next(
        h
        for h in lp.load_pool(
            {
                "LANDING_POOL_HOSTS": json.dumps(
                    [
                        {
                            "name": "mac",
                            "local": True,
                            "engines": ["claude_sonnet", "claude_opus"],
                        },
                        {
                            "name": "n101",
                            "target": "198.51.100.101",
                            "engines": ["claude_sonnet", "claude_opus"],
                        },
                        {
                            "name": "n202",
                            "target": "198.51.100.202",
                            "engines": ["claude_sonnet", "claude_opus"],
                        },
                    ]
                )
            }
        )
        if h.name == name
    )
    return lp.Reading(host=host, **over)


def h101_admitted_reading(**over: Any) -> Any:
    base: dict[str, Any] = {
        "cores": 12,
        "load1": 24.7,
        "cpu_idle_pct": 58.7,
        "busy_cores": 4.96,
        "mem_avail_gb": 12.0,
        "mem_pressure": 1,
    }
    base.update(over)
    return lab("n101", **base)


def test_h101_admitted_on_the_recorded_numbers() -> None:
    n101 = h101_admitted_reading()
    assert n101.load_refusal is None
    assert n101.admission_refusal is None
    assert n101.slots >= 1
    assert "ADMISSION-REFUSED" not in n101.describe()


def test_h101_admitted_at_its_live_load_43() -> None:
    n101 = h101_admitted_reading(load1=43.06)
    assert n101.admission_refusal is None
    assert n101.slots >= 1


def test_the_probe_parses_the_recorded_h101_top_into_an_admitted_reading() -> None:
    text = "LANGRANT ok\nPROBE 12 24.7 12582912\nCPUIDLE 58.7\nPRESSURE 1\nCOAUTHOR false\nLOGIN 1 1\n"
    reading = lp.parse_probe(lab("n101").host, text, 0)
    assert reading.cpu_idle_pct == 58.7
    assert reading.busy_cores == pytest.approx(12 * (1 - 0.587), abs=0.01)
    assert reading.admission_refusal is None
    assert reading.slots >= 1


def test_a_cpu_saturated_mac_host_is_refused_whatever_its_load1() -> None:
    sat = h101_admitted_reading(load1=9.0, cpu_idle_pct=7.4, busy_cores=11.1)
    assert sat.load_refusal is not None
    assert "CPU" in sat.load_refusal
    assert sat.slots == 0
    assert sat.lane_slots == 0
    assert "ADMISSION-REFUSED" in sat.describe()


def test_a_mac_host_over_the_secondary_load_bar_is_refused_whatever_its_idle() -> None:
    wedged = h101_admitted_reading(load1=90.0, cpu_idle_pct=58.7)
    assert wedged.load_refusal is not None
    assert "LOAD-OVER-BAR" in wedged.load_refusal
    assert wedged.slots == 0


def test_the_idle_bar_sits_at_25_percent() -> None:
    assert h101_admitted_reading(cpu_idle_pct=25.0).load_refusal is None
    assert h101_admitted_reading(cpu_idle_pct=24.9).load_refusal is not None


def test_memory_pressure_still_refuses_an_idle_mac_host() -> None:
    pressed = h101_admitted_reading(mem_pressure=2)
    assert pressed.admission_refusal is not None
    assert "memory pressure" in pressed.admission_refusal
    assert pressed.slots == 0


def test_a_mac_host_with_no_idle_sample_keeps_the_load_bar() -> None:
    unsampled = lab("n101", cores=12, load1=24.7, mem_avail_gb=12.0, mem_pressure=1)
    assert unsampled.load_refusal is not None
    assert "LOAD-OVER-BAR" in unsampled.load_refusal


def test_a_linux_host_is_still_refused_on_load1_whatever_its_idle() -> None:
    n202 = lab(
        "n202", cores=32, load1=59.0, busy_cores=5.0, mem_avail_gb=40.0, placed=20
    )
    assert n202.cpu_idle_pct is None
    assert n202.load_refusal is not None
    assert "LOAD-OVER-BAR" in n202.load_refusal


@pytest.mark.parametrize(
    ("top_out", "want"), [(H101_TOP, "58.7"), (SATURATED_TOP, "7.4")]
)
def test_the_probe_reads_the_second_top_sample(
    tmp_path: Path, top_out: str, want: str
) -> None:
    """The probe's macOS CPU snippet takes the LAST `CPU usage` line (the live sample), not the since-boot first."""
    fake = tmp_path / "top"
    fixture = tmp_path / "top.out"
    fixture.write_text(top_out, encoding="utf-8")
    fake.write_text(f'#!/bin/sh\ncat "{fixture}"\n', encoding="utf-8")
    fake.chmod(0o755)
    script = lp.MAC_CPU_IDLE_SH + '\necho "CPUIDLE ${ci:-none}"\n'
    out = subprocess.run(
        ["/bin/sh", "-c", script],
        capture_output=True,
        text=True,
        check=True,
        env={"PATH": f"{tmp_path}:/usr/bin:/bin"}
        | {k: v for k, v in os.environ.items() if k == "HOME"},
    ).stdout
    assert out.strip() == f"CPUIDLE {want}"


def test_the_snippet_is_part_of_the_probe_for_macos_only() -> None:
    mac_branch = lp._PROBE.split("else\n", 1)[1]
    assert lp.MAC_CPU_IDLE_SH.strip().splitlines()[0] in mac_branch
    assert 'echo "CPUIDLE $ci"' in lp._PROBE
