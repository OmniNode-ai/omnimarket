# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Placement never reaches a host the table marks ``available: false`` and refuses a host over its load bar whatever its
cap says (OMN-20366, plan item P7). Offline: ssh and ping are replaced by a recorder."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_landing_placement_effect.handlers import (
    handler_landing_placement as lp,
)

UNAVAILABLE = "192.0.2.35"
# The shape of the private data clone's lab host table on 2026-10-02: n105 out of the mix, n202 with a declared cap of 28.
TABLE = """\
hosts:
  - name: n200
    target: local
    local: true
  - name: n105
    target: 192.0.2.35
    available: false
    unavailable_reason: "operator: out of the mix"
  - name: n101
    target: 192.0.2.31
  - name: n202
    target: 192.0.2.42
    penalty: 0.1
    cap: 28
  - name: n201
    target: 192.0.2.41
    penalty: 0.25
"""


@pytest.fixture
def table_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    table = tmp_path / "lab_run_hosts.yaml"
    table.write_text(TABLE, encoding="utf-8")
    monkeypatch.delenv("LANDING_POOL_HOSTS", raising=False)
    monkeypatch.setenv("ONEX_LAB_RUN_HOSTS", str(table))
    monkeypatch.setenv("LANDING_PLACEMENT_DIR", str(tmp_path / "placement"))
    return table


@pytest.fixture
def trace(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every subprocess the placement starts (a probe over ssh, a ping, the local probe), as one string each."""
    seen: list[str] = []

    def run(argv: Any, **_kw: Any) -> subprocess.CompletedProcess[str]:
        seen.append(" ".join(str(a) for a in argv))
        return subprocess.CompletedProcess(argv, 255, "", "unreachable")

    monkeypatch.setattr(subprocess, "run", run)
    return seen


def test_a_landing_pool_hosts_override_honors_available_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        {"name": "n105", "target": UNAVAILABLE, "engines": "live", "available": False},
        {"name": "n201", "target": "192.0.2.41", "engines": "live"},
    ]
    monkeypatch.setenv("LANDING_POOL_HOSTS", json.dumps(rows))
    assert [h.name for h in lp.load_pool()] == ["n201"]


def lab(name: str, **over: Any) -> Any:
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
                            "name": "n201",
                            "target": "198.51.100.201",
                            "engines": ["claude_sonnet", "claude_opus"],
                            "penalty": 0.25,
                        },
                        {
                            "name": "n202",
                            "target": "198.51.100.202",
                            "engines": ["claude_sonnet", "claude_opus"],
                            "penalty": 0.1,
                            "cap": 28,
                        },
                        {
                            "name": "n101",
                            "target": "198.51.100.101",
                            "engines": ["claude_sonnet", "claude_opus"],
                        },
                    ]
                )
            }
        )
        if h.name == name
    )
    return lp.Reading(host=host, **over)


def test_description_round_trip_preserves_admission_refusal() -> None:
    r = lab(
        "n202",
        cores=32,
        load1=59.0,
        busy_cores=5.0,
        mem_avail_gb=40.0,
        swap_used_frac=0.3,
        mem_psi_avg60=6.0,
        cpu_temp_c=92.8,
        dimm_temp_c=71.0,
        placed=3,
    )
    h = r.host
    parsed = lp.Reading.from_description(h, r.describe())
    assert parsed is not None
    assert parsed.admission_refusal == r.admission_refusal


def test_description_round_trip_preserves_healthy_admission() -> None:
    r = lab("n201", cores=32, load1=4.0, mem_avail_gb=40.0, cpu_temp_c=70.0)
    parsed = lp.Reading.from_description(r.host, r.describe())
    assert parsed is not None
    assert parsed.admission_refusal == r.admission_refusal is None
    assert lp.Reading.from_description(r.host, "n201:UNREADABLE(probe failed)") is None


def test_a_host_over_its_load_bar_is_refused_whatever_its_cap_and_busy_sample_say() -> (
    None
):
    """n202 on 2026-10-02 ~12:30 ET: load1 59 on 32 cores, 20 sessions placed, cap 28. The busy sample reads the host
    as nearly idle (iowait), so the slot arithmetic alone admitted more work."""
    n202 = lab(
        "n202", cores=32, load1=59.0, busy_cores=5.0, mem_avail_gb=40.0, placed=20
    )
    assert n202.cap == 28
    assert n202.free_cores > 0, "the arithmetic alone would still admit work here"
    assert n202.load_refusal is not None
    assert "LOAD-OVER-BAR" in n202.load_refusal
    assert n202.admission_refusal == n202.lane_admission_refusal == n202.load_refusal
    assert n202.slots == 0
    assert n202.lane_slots == 0
    assert "ADMISSION-REFUSED(LOAD-OVER-BAR" in n202.describe()


def test_the_bar_leaves_an_iowait_bound_host_that_the_busy_sample_admits() -> None:
    """n202 on 2026-09-30: load 40 on 32 cores (1.25 per core) with the CPU mostly idle is not overload."""
    n202 = lab(
        "n202", cores=32, load1=40.0, busy_cores=6.4, mem_avail_gb=40.0, placed=3
    )
    assert n202.load_refusal is None
    assert n202.slots > 0


def test_the_launching_host_over_the_load_bar_takes_no_lane() -> None:
    mac = lab("mac", cores=24, load1=40.0, busy_cores=2.0, mem_avail_gb=40.0)
    assert mac.load_refusal is not None
    assert mac.lane_slots == 0


def test_ranking_follows_load1_so_an_iowait_bound_host_does_not_outrank_an_idle_one() -> (
    None
):
    busy_looking_idle = lab(
        "n202", cores=32, load1=45.0, busy_cores=3.0, mem_avail_gb=40.0, placed=2
    )
    idle = lab("n201", cores=32, load1=10.0, mem_avail_gb=62.0)
    assert busy_looking_idle.slots > 0, "both pass the bar"
    assert idle.slots > 0, "both pass the bar"
    pick = lp.choose_lane("claude_sonnet", [busy_looking_idle, idle])
    assert pick is not None
    assert pick.host.name == "n201"
    pick = lp.choose("claude_sonnet", [busy_looking_idle, idle])
    assert pick is not None
    assert pick.host.name == "n201"
