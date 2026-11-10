# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_host_installer_registry_units: the landing snapshot step is a declared launchd unit on the controller's host (OMN-23458). Until this unit existed the step ran only by hand; at the merge base the installer knows no landing-snapshot unit and the render test fails."""

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
    canonical,  # noqa: F401
    make_checkout,
    plist_of,
    request,
)

pytestmark = pytest.mark.unit

UNIT = "landing-snapshot"
ENTRY = "src/omnimarket/nodes/node_pr_landing_snapshot_effect/__main__.py"


@pytest.fixture(autouse=True)
def brew_python(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units.interpreter_present",
        lambda path: path == BREW_PYTHON,
    )
    monkeypatch.setattr(
        shutil, "which", lambda name: "/usr/bin/uv" if name == "uv" else None
    )


def test_the_unit_is_declared_every_minute_on_launchd_and_runs_the_step_module() -> (
    None
):
    unit = load_units()[UNIT]
    assert unit["platform"] == "launchd"
    assert unit["schedule"]["start_interval"] == 60
    assert unit["module"] == "omnimarket.nodes.node_pr_landing_snapshot_effect"
    assert unit["entry_file"] == ENTRY
    assert unit["run_timeout_s"] < 60
    assert unit["environment"]["ONEX_SKILL_OVERLAY_ROOTS"].startswith(
        "{registry_root}/overlays/overlays"
    )
    assert unit["environment"]["OMNIBASE_PATH"] == "{registry_root}"


@pytest.mark.usefixtures("canonical")
def test_install_renders_the_unit_running_the_step_from_the_branch_checkout(
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
    assert plist["Label"] == "ai.omninode.landing-snapshot"
    assert plist["StartInterval"] == 60
    assert plist["RunAtLoad"] is True
    args = plist["ProgramArguments"]
    assert (
        args[args.index("-m") + 1] == "omnimarket.nodes.node_pr_landing_snapshot_effect"
    )
    assert str(branch) in args
    env = plist["EnvironmentVariables"]
    assert env["OMNIBASE_PATH"] == str(tmp_path / "omni")
    assert env["ONEX_SKILL_OVERLAY_ROOTS"].startswith(
        str(tmp_path / "omni" / "overlays" / "overlays")
    )


@pytest.mark.usefixtures("canonical")
def test_a_checkout_without_the_step_entry_file_is_refused(tmp_path: Path) -> None:
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
