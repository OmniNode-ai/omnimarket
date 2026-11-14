# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_host_installer_registry_units: the lane host pool snapshot is a declared 60-second launchd unit on a macOS pool host (OMN-23725).

The systemd unit covered the Linux pool hosts only; a macOS pool host had no declared unit to take. At the merge base the installer
knows no lane-host-pool-snapshot launchd unit and every test below fails.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    HandlerRegistryUnits,
    load_units,
)
from tests.nodes.node_host_installer_registry_units.test_registry_units import (
    BREW_PYTHON,
    Recorder,
    make_checkout,
    plist_of,
    request,
)

pytestmark = pytest.mark.unit

UNIT = "lane-host-pool-snapshot"
SYSTEMD_TWIN = "lane-host-pool-snapshot-systemd"
ENTRY = "src/omnimarket/nodes/node_lane_host_pool_snapshot_effect/__main__.py"
MAX_GAP_SECONDS = 300


@pytest.fixture
def canonical(tmp_path: Path) -> Path:
    path = tmp_path / "omni" / "omnimarket"
    make_checkout(path)
    return path


@pytest.fixture(autouse=True)
def brew_python(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units.interpreter_present",
        lambda path: path == BREW_PYTHON,
    )
    monkeypatch.setattr(
        shutil, "which", lambda name: "/usr/bin/uv" if name == "uv" else None
    )


def test_the_unit_is_declared_every_minute_on_launchd_and_runs_the_snapshot_module() -> (
    None
):
    unit = load_units()[UNIT]
    assert unit["platform"] == "launchd"
    assert unit["schedule"]["start_interval"] == 60
    assert unit["schedule"]["start_interval"] <= MAX_GAP_SECONDS
    assert unit["schedule"]["run_at_load"] is True
    assert unit["module"] == "omnimarket.nodes.node_lane_host_pool_snapshot_effect"
    assert unit["entry_file"] == ENTRY
    assert unit["run_timeout_s"] < unit["schedule"]["start_interval"]
    assert unit["probe_argv"] == ["--probe"]
    assert unit["environment"]["ONEX_SKILL_OVERLAY_ROOTS"] == "{overlays_root}"
    assert unit["environment"]["OMNIBASE_PATH"] == "{registry_root}"


def test_both_platform_twins_run_the_same_module_at_the_same_cadence() -> None:
    units = load_units()
    mac, linux = units[UNIT], units[SYSTEMD_TWIN]
    assert mac["module"] == linux["module"]
    assert mac["entry_file"] == linux["entry_file"]
    assert mac["schedule"]["start_interval"] == linux["schedule"]["start_interval"]
    assert (
        mac["environment"]["ONEX_LEDGER_BUS_READ_COMMAND"]
        == linux["environment"]["ONEX_LEDGER_BUS_READ_COMMAND"]
    )


@pytest.mark.usefixtures("canonical")
def test_install_renders_a_60_second_agent_running_the_module_from_the_branch_checkout(
    tmp_path: Path,
) -> None:
    branch = tmp_path / "branch"
    revision = make_checkout(branch, entry=ENTRY)
    result = HandlerRegistryUnits(Recorder()).handle(
        request(
            tmp_path,
            unit=UNIT,
            mode="install",
            project_path=str(branch),
            expected_revision=revision,
        )
    )
    assert result.status == "success", result.stderr
    plist = plist_of(result.rendered)
    assert plist["Label"] == "ai.omninode.lane-host-pool-snapshot"
    assert plist["StartInterval"] == 60
    assert plist["RunAtLoad"] is True
    args = plist["ProgramArguments"]
    assert (
        args[args.index("-m") + 1]
        == "omnimarket.nodes.node_lane_host_pool_snapshot_effect"
    )
    assert str(branch) in args
    env = plist["EnvironmentVariables"]
    assert env["OMNIBASE_PATH"] == str(tmp_path / "omni")
    assert env["ONEX_SKILL_OVERLAY_ROOTS"] == str(
        tmp_path / "omni" / "overlays" / "overlays"
    )
    assert env["PATH"].startswith(str(tmp_path / "omni" / "omnibase_infra" / "scripts"))
    assert "/opt/homebrew/bin" in env["PATH"].split(":")
    assert env["ONEX_LEDGER_BUS_READ_COMMAND"].endswith("onex ledger query --stdin")


@pytest.mark.usefixtures("canonical")
def test_a_checkout_without_the_snapshot_entry_file_is_refused(tmp_path: Path) -> None:
    """Error chain: the installer refuses a branch that does not carry the snapshot's entry file."""
    branch = tmp_path / "branch"
    make_checkout(branch, entry="src/omnimarket/nodes/other/__main__.py")
    result = HandlerRegistryUnits(Recorder()).handle(
        request(
            tmp_path,
            unit=UNIT,
            mode="install",
            project_path=str(branch),
        )
    )
    assert result.status == "failure"
