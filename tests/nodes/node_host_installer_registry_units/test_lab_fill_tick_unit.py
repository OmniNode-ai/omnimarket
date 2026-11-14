# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_host_installer_registry_units: the lab fill is a declared 1200-second launchd unit that runs the tick entry (OMN-24419).

The Mac lab-fill job ran the retired lab-fill.js through a model session and ended with exit 70. The installer now declares the job
under its own label and runs omnimarket's runtime_lab_fill_tick. At the merge base the installer knows no lab-fill-tick unit and every
test below fails.
"""

from __future__ import annotations

import importlib
import io
import json
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

UNIT = "lab-fill-tick"
MODULE = "omnimarket.nodes.node_lab_fill_effect.runtime_lab_fill_tick"
ENTRY = "src/omnimarket/nodes/node_lab_fill_effect/runtime_lab_fill_tick.py"
FACTORY_FLOOR_EXPECTED_INTERVAL_SECONDS = 1200


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


def test_the_unit_is_declared_every_1200_seconds_on_launchd_and_runs_the_tick_entry() -> (
    None
):
    unit = load_units()[UNIT]
    assert unit["platform"] == "launchd"
    assert unit["label"] == "ai.omninode.lab-fill"
    assert unit["schedule"]["start_interval"] == FACTORY_FLOOR_EXPECTED_INTERVAL_SECONDS
    assert unit["schedule"]["run_at_load"] is True
    assert unit["module"] == MODULE
    assert unit["entry_file"] == ENTRY
    assert unit["run_timeout_s"] < unit["schedule"]["start_interval"]
    assert unit["probe_argv"] == ["--probe"]
    assert unit["environment"]["ONEX_SKILL_OVERLAY_ROOTS"].startswith(
        "{overlays_root}:"
    )
    assert unit["environment"]["OMNIBASE_PATH"] == "{registry_root}"


def test_the_declared_entry_file_and_module_exist_and_the_probe_answers() -> None:
    unit = load_units()[UNIT]
    module = importlib.import_module(unit["module"])
    assert (Path(__file__).parents[3] / unit["entry_file"]).is_file()
    out = io.StringIO()
    assert module.main(unit["probe_argv"], environ={}, stdout=out) == 0
    assert json.loads(out.getvalue()) == {"status": "probe_ok"}


@pytest.mark.usefixtures("canonical")
def test_install_renders_a_1200_second_agent_running_the_module_from_the_branch_checkout(
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
    assert plist["Label"] == "ai.omninode.lab-fill"
    assert plist["StartInterval"] == FACTORY_FLOOR_EXPECTED_INTERVAL_SECONDS
    assert plist["RunAtLoad"] is True
    args = plist["ProgramArguments"]
    assert args[args.index("-m") + 1] == MODULE
    assert str(branch) in args
    assert "lab-fill.js" not in result.rendered
    env = plist["EnvironmentVariables"]
    assert env["OMNIBASE_PATH"] == str(tmp_path / "omni")
    assert env["ONEX_SKILL_OVERLAY_ROOTS"].startswith(
        str(tmp_path / "omni" / "overlays" / "overlays")
    )
    assert env["PATH"].startswith(str(tmp_path / "omni" / "omnibase_infra" / "scripts"))


@pytest.mark.usefixtures("canonical")
def test_a_checkout_without_the_tick_entry_file_is_refused(tmp_path: Path) -> None:
    """Error chain: the installer refuses a branch that does not carry the lab-fill entry file."""
    branch = tmp_path / "branch"
    make_checkout(branch, entry="src/omnimarket/nodes/other/__main__.py")
    result = HandlerRegistryUnits(Recorder()).handle(
        request(tmp_path, unit=UNIT, mode="install", project_path=str(branch))
    )
    assert result.status == "failure"
