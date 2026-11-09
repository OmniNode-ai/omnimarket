# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""lab:hosts reads a host busy with its own placed units as fine, and every other refusal as failing (OMN-22870).

Golden: the pool's refusal read on the floor at 04:47Z, CPU idle under the bar with the one unit placed there explaining the
load, does not turn the row red. Error chain: the named injected breaks that must still read red.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from omnimarket.models.floor_17a.model_floor_17a import ModelFloorLabLine
from tests.nodes.node_floor_simple_effect.support import (
    OVERLAYS,
    build_home,
    environ,
    record,
)
from tests.nodes.node_floor_simple_effect.test_golden_chain_floor_simple import (
    run,
    written,
)

pytestmark = pytest.mark.unit

OWN_LOAD = (
    "CPU-IDLE-UNDER-BAR: 21.6% idle < 25%; RESERVE-OWN-LOAD: the 1 units placed here "
    "explain the load (load1 3.2 less 2 a unit is 1.2, not over 8)"
)


def hosts_line(
    tmp_path: Path, reason: str, *, marker_off: bool = False
) -> ModelFloorLabLine:
    rec = record()
    rec["floor"]["machines"][0]["admission"] = {"state": "refused", "reason": reason}
    home = build_home(tmp_path, rec=rec)
    env = environ(home)
    if marker_off:
        roots = tmp_path / "overlay-roots"
        shutil.copytree(OVERLAYS, roots)
        path = roots / "node_floor_simple_effect" / "overlay.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        data["own_load_marker"] = ""
        path.write_text(yaml.safe_dump(data), encoding="utf-8")
        env["ONEX_SKILL_OVERLAY_ROOTS"] = str(roots)
    run(home, env=env)
    return next(x for x in written(home).lab_state if x.id == "hosts")


def test_golden_a_host_busy_only_with_its_own_units_reads_fine(tmp_path: Path) -> None:
    line = hosts_line(tmp_path, OWN_LOAD)
    assert line.state == "WORKING"
    assert line.text == "3 of 3 hosts fine"


def test_error_a_load_nothing_placed_explains_still_reads_failing(
    tmp_path: Path,
) -> None:
    line = hosts_line(
        tmp_path,
        "CPU-IDLE-UNDER-BAR: 21.6% idle < 25%; RESERVE-HELD: 4 placed >= reserve 2",
    )
    assert line.state == "NOT WORKING"
    assert "h051" in line.text


def test_error_b_own_load_with_a_memory_refusal_still_reads_failing(
    tmp_path: Path,
) -> None:
    line = hosts_line(tmp_path, f"mem_avail 8.7G < 10G; {OWN_LOAD}")
    assert line.state == "NOT WORKING"
    assert "mem_avail" in line.text


def test_error_c_the_overlay_marker_off_reads_every_refusal_failing(
    tmp_path: Path,
) -> None:
    line = hosts_line(tmp_path, OWN_LOAD, marker_off=True)
    assert line.state == "NOT WORKING"
    assert "CPU-IDLE-UNDER-BAR" in line.text
