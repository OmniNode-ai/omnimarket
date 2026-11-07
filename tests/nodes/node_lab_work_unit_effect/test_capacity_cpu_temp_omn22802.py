# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A Linux host's capacity advertisement carries its CPU temperature as a pseudo-tool (OMN-22803)."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_lab_work_unit_effect.protocols.local_host_reader import (
    CPU_TEMP_TOOL_PREFIX,
    cpu_temp_from_tools,
    linux_cpu_temp_c,
)

pytestmark = pytest.mark.unit


def _chip(root: Path, index: int, name: str, labels: dict[str, int]) -> None:
    chip = root / f"hwmon{index}"
    chip.mkdir(parents=True)
    (chip / "name").write_text(f"{name}\n")
    for number, (label, milli) in enumerate(labels.items(), start=1):
        (chip / f"temp{number}_label").write_text(f"{label}\n")
        (chip / f"temp{number}_input").write_text(f"{milli}\n")


def test_the_cpu_package_reading_is_read_from_k10temp(tmp_path: Path) -> None:
    _chip(tmp_path, 0, "nvme", {"Composite": 41000})
    _chip(tmp_path, 1, "k10temp", {"Tctl": 90125, "Tccd1": 70000})
    assert linux_cpu_temp_c(tmp_path) == pytest.approx(90.125)


def test_coretemp_package_id_is_read(tmp_path: Path) -> None:
    _chip(tmp_path, 0, "coretemp", {"Package id 0": 63000, "Core 0": 60000})
    assert linux_cpu_temp_c(tmp_path) == pytest.approx(63.0)


def test_a_host_with_no_cpu_sensor_reads_none(tmp_path: Path) -> None:
    _chip(tmp_path, 0, "nvme", {"Composite": 41000})
    assert linux_cpu_temp_c(tmp_path) is None
    assert linux_cpu_temp_c(tmp_path / "absent") is None


def test_the_pseudo_tool_round_trips_and_a_bad_one_reads_none() -> None:
    assert cpu_temp_from_tools(["git", f"{CPU_TEMP_TOOL_PREFIX}83.4"]) == 83.4
    assert cpu_temp_from_tools(["git", "uv"]) is None
    assert cpu_temp_from_tools([f"{CPU_TEMP_TOOL_PREFIX}warm"]) is None
