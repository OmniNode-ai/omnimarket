# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_host_installer_registry_units: the cutover repositories' landing units (OMN-23514).

Three launchd units on the floor host, each every 60 s: landing-watcher-facts writes the cutover
repositories' facts from the PR watcher state, landing-snapshot-cutover (a second instance of the
snapshot step, its own overlay file pinned) records them, and landing-tick-orchestrator compares each
record in observe mode. At the merge base the installer knows none of the three and the render test fails.
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

UNITS = {
    "landing-watcher-facts": "node_pr_landing_watcher_facts_effect",
    "landing-snapshot-cutover": "node_pr_landing_snapshot_effect",
    "landing-tick-orchestrator": "node_pr_landing_tick_orchestrator",
}
PIN_ENV = "ONEX_LANDING_SNAPSHOT_OVERLAY_FILE"


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


@pytest.mark.parametrize(("unit", "node"), sorted(UNITS.items()))
def test_each_unit_is_declared_every_minute_and_runs_its_node(
    unit: str, node: str
) -> None:
    declared = load_units()[unit]
    assert declared["platform"] == "launchd"
    assert declared["schedule"]["start_interval"] == 60
    assert declared["module"] == f"omnimarket.nodes.{node}"
    assert declared["entry_file"] == f"src/omnimarket/nodes/{node}/__main__.py"
    assert declared["run_timeout_s"] < 60
    assert declared["environment"]["ONEX_SKILL_OVERLAY_ROOTS"].startswith(
        "{registry_root}/overlays/overlays"
    )


def test_the_cutover_snapshot_unit_pins_its_own_overlay_and_the_first_does_not() -> (
    None
):
    units = load_units()
    pin = units["landing-snapshot-cutover"]["environment"][PIN_ENV]
    assert pin == (
        "{overlay_root}/node_pr_landing_snapshot_effect_cutover/overlay.yaml"
    )
    assert (
        PIN_ENV
        in units["landing-snapshot-cutover"]["environment_refuse_worktree_paths"]
    )
    # the controller's instance keeps reading the node's own overlay
    assert PIN_ENV not in units["landing-snapshot"]["environment"]


@pytest.mark.usefixtures("canonical")
@pytest.mark.parametrize(("unit", "node"), sorted(UNITS.items()))
def test_install_renders_each_unit_from_the_branch_checkout(
    tmp_path: Path, unit: str, node: str
) -> None:
    branch = tmp_path / "branch"
    entry = f"src/omnimarket/nodes/{node}/__main__.py"
    revision = make_checkout(branch, entry=entry)
    result = HandlerRegistryUnits(Recorder()).handle(
        request(
            tmp_path,
            unit=unit,
            mode="install",
            project_path=str(branch),
            expected_revision=revision,
        )
    )
    assert result.status == "success", result.stderr
    plist = plist_of(result.rendered)
    assert plist["Label"] == f"ai.omninode.{unit}"
    assert plist["StartInterval"] == 60
    args = plist["ProgramArguments"]
    assert args[args.index("-m") + 1] == f"omnimarket.nodes.{node}"
    assert str(branch) in args
    env = plist["EnvironmentVariables"]
    assert env["OMNIBASE_PATH"] == str(tmp_path / "omni")
    if unit == "landing-snapshot-cutover":
        assert env[PIN_ENV] == str(
            tmp_path
            / "omni"
            / "overlays"
            / "overlays"
            / "node_pr_landing_snapshot_effect_cutover"
            / "overlay.yaml"
        )
