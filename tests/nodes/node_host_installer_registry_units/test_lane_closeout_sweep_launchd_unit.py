# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lane closeout sweep is also a declared launchd unit, for a macOS lab host whose registry root is on an external volume (OMN-23404)."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    load_units,
)
from tests.nodes.node_host_installer_registry_units.test_log_directory import (
    BREW_PYTHON,
    READER,
    READER_ENTRY,
    render,
)


@pytest.fixture(autouse=True)
def brew_python(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units.interpreter_present",
        lambda path: path == BREW_PYTHON,
    )


SWEEP = "lane-closeout-sweep"
SWEEP_ENTRY = "src/omnimarket/nodes/node_lane_closeout_sweep_effect/runtime_lane_closeout_sweep.py"
OVERRIDES = {"LAB_WORK_HOST_NAME": "h101"}


def test_the_launchd_unit_declares_a_run_limit_under_its_interval() -> None:
    declared = load_units()[SWEEP]
    assert declared["platform"] == "launchd"
    assert 0 < declared["run_timeout_s"] <= declared["schedule"]["start_interval"]


def test_golden_the_job_stands_in_the_home_but_the_tick_reads_the_registry_root(
    tmp_path: Path,
) -> None:
    plist = render(
        tmp_path,
        SWEEP,
        SWEEP_ENTRY,
        path_overrides={
            "ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "overlays"),
            **OVERRIDES,
        },
    )
    job_root = str(tmp_path / "home/.local/state/omni/lane-closeout-sweep")
    arguments = plist["ProgramArguments"]
    assert plist["WorkingDirectory"] == job_root
    assert plist["EnvironmentVariables"]["OMNI_HOME"] == job_root
    assert arguments[arguments.index("--omni-home") + 1] == str(tmp_path / "omni")
    assert plist["EnvironmentVariables"]["LAB_WORK_HOST_NAME"] == "h101"
    assert "ONEX_SKILL_OVERLAY_ROOTS" in plist["EnvironmentVariables"]
    assert arguments[arguments.index("-m") + 1] == (
        "omnimarket.nodes.node_lane_closeout_sweep_effect.runtime_lane_closeout_sweep"
    )
    assert plist["StartInterval"] == 300


def test_error_a_unit_without_tick_omni_home_passes_its_job_root_to_the_tick(
    tmp_path: Path,
) -> None:
    """Failing control: the overlay reader declares launchd_root and no tick_omni_home, so its tick gets the job root."""
    assert "tick_omni_home" not in load_units()[READER]
    plist = render(
        tmp_path,
        READER,
        READER_ENTRY,
        path_overrides={
            "ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "overlays"),
            **OVERRIDES,
        },
    )
    arguments = plist["ProgramArguments"]
    root = str(tmp_path / "home/.local/state/omni/overlay-store-refresh")
    assert arguments[arguments.index("--omni-home") + 1] == root
