# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_host_installer_registry_units: the host retention uv cache launchd unit, the Mac form of the systemd unit."""

from __future__ import annotations

import re
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

HANDLER = "omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units"
UNIT = "host-retention-uv-cache-launchd"
SYSTEMD_UNIT = "host-retention-uv-cache-systemd"
MODULE = "omnimarket.nodes.node_host_retention_effect.runtime_host_retention_tick"
ENTRY = "src/omnimarket/nodes/node_host_retention_effect/runtime_host_retention_tick.py"
ONE_DAY = 86400
CREDENTIAL_NAME = re.compile(
    r"TOKEN|SECRET|PASSWORD|CREDENTIAL|API_KEY|PRIVATE_KEY", re.I
)


@pytest.fixture(autouse=True)
def brew_python(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        f"{HANDLER}.interpreter_present", lambda path: path == BREW_PYTHON
    )
    monkeypatch.setattr(
        shutil, "which", lambda name: "/usr/bin/uv" if name == "uv" else None
    )


def test_golden_the_unit_declares_the_same_tick_as_the_systemd_unit_on_launchd() -> (
    None
):
    units = load_units()
    unit = units[UNIT]
    systemd = units[SYSTEMD_UNIT]
    assert unit["platform"] == "launchd"
    assert unit["module"] == systemd["module"] == MODULE
    assert unit["entry_file"] == systemd["entry_file"] == ENTRY
    assert (Path(__file__).parents[3] / ENTRY).is_file()
    assert unit["probe_argv"] == ["--probe"]
    for field in (
        "environment",
        "environment_inputs",
        "required_environment_inputs",
        "environment_path_overrides",
        "environment_refuse_worktree_paths",
        "evidence_file",
        "state_directory",
    ):
        assert unit[field] == systemd[field], field
    assert unit["environment"]["ONEX_SKILL_OVERLAY_ROOTS"] == "{overlays_root}"
    assert unit["environment_inputs"] == {"ONEX_HOST_NAME": "ledger_host_name"}
    assert unit["run_timeout_s"] == systemd["schedule"]["run_timeout_s"] == 3600
    assert not CREDENTIAL_NAME.search("\n".join(unit["environment"]))


def test_the_mac_cadence_is_daily_and_no_more_frequent_than_the_linux_unit() -> None:
    units = load_units()
    mac = units[UNIT]["schedule"]
    assert mac["start_interval"] == ONE_DAY
    assert mac["run_at_load"] is False
    assert mac["start_interval"] >= units[SYSTEMD_UNIT]["schedule"]["start_interval"]


def test_golden_the_rendered_agent_runs_the_tick_module_with_the_host_name(
    tmp_path: Path,
) -> None:
    unit = load_units()[UNIT]
    branch = tmp_path / "branch"
    revision = make_checkout(branch, entry=unit["entry_file"])
    result = HandlerRegistryUnits(Recorder()).handle(
        request(
            tmp_path,
            unit=UNIT,
            mode="dry-run",
            project_path=str(branch),
            expected_revision=revision,
            ledger_host_name="h101",
        )
    )
    assert result.status == "success", result.stderr
    plist = plist_of(result.rendered)
    assert plist["Label"] == "ai.omninode.host-retention-uv-cache"
    assert plist["StartInterval"] == ONE_DAY
    assert plist["RunAtLoad"] is False
    args = plist["ProgramArguments"]
    assert args[args.index("-m") + 1] == MODULE
    assert args[args.index("--project") + 1] == str(branch)
    assert args[args.index("--omni-home") + 1] == str(tmp_path / "omni")
    assert args[args.index("--state-dir") + 1] == str(
        (tmp_path / "home" / unit["evidence_file"]).parent
    )
    assert str(unit["run_timeout_s"]) in args
    env = plist["EnvironmentVariables"]
    assert env["ONEX_HOST_NAME"] == "h101"
    assert env["ONEX_SKILL_OVERLAY_ROOTS"] == str(
        tmp_path / "omni" / "overlays" / "overlays"
    )


def test_a_checkout_without_the_tick_entry_file_is_refused(tmp_path: Path) -> None:
    branch = tmp_path / "branch"
    make_checkout(branch, entry="src/omnimarket/nodes/other/__main__.py")
    result = HandlerRegistryUnits(Recorder()).handle(
        request(tmp_path, unit=UNIT, mode="install", project_path=str(branch))
    )
    assert result.status == "failure"
