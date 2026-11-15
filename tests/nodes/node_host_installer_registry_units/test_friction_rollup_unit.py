# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_host_installer_registry_units: the friction rollup tick is a declared 7200-second launchd unit (OMN-24574).

Until this unit existed the rollup ran from a copied tree the private host installer installed under the same
label. The installer now declares the job and runs omnimarket's runtime_friction_rollup_tick, so an installed
unit may run a branch checkout and records the revision it runs. At the merge base the installer knows no
friction-rollup unit and every test below fails.
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

UNIT = "friction-rollup"
MODULE = (
    "omnimarket.nodes.node_friction_rollup_tick_effect.runtime_friction_rollup_tick"
)
ENTRY = "src/omnimarket/nodes/node_friction_rollup_tick_effect/runtime_friction_rollup_tick.py"


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


def test_the_unit_is_declared_every_two_hours_on_launchd_and_runs_the_rollup_module() -> (
    None
):
    unit = load_units()[UNIT]
    assert unit["platform"] == "launchd"
    assert unit["schedule"]["start_interval"] == 7200
    assert unit["schedule"]["run_at_load"] is False
    assert unit["module"] == MODULE
    assert unit["entry_file"] == ENTRY
    assert unit["run_timeout_s"] < 7200
    assert unit["environment"]["ONEX_SKILL_OVERLAY_ROOTS"].startswith("{overlays_root}")


@pytest.mark.usefixtures("canonical")
def test_install_renders_a_7200_second_agent_running_the_module_from_the_branch_checkout(
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
    assert plist["Label"] == "ai.omninode.friction-rollup"
    assert plist["StartInterval"] == 7200
    assert plist["RunAtLoad"] is False
    args = plist["ProgramArguments"]
    assert args[args.index("-m") + 1] == MODULE
    assert args[args.index("--omni-home") + 1] == str(tmp_path / "omni")
    env = plist["EnvironmentVariables"]
    assert env["ONEX_SKILL_OVERLAY_ROOTS"].startswith(
        str(tmp_path / "omni" / "overlays" / "overlays")
    )


@pytest.mark.usefixtures("canonical")
def test_a_checkout_without_the_tick_entry_file_is_refused(tmp_path: Path) -> None:
    """Error chain: the installer refuses a branch that does not carry the friction rollup entry file."""
    branch = tmp_path / "branch"
    make_checkout(branch, entry="src/omnimarket/nodes/other/runtime_other.py")
    recorder = Recorder()
    result = HandlerRegistryUnits(recorder).handle(
        request(tmp_path, unit=UNIT, mode="install", project_path=str(branch))
    )
    assert result.status == "failure"
    # The checkout is checked before anything is loaded, so no launchctl call ran. The id and git
    # calls of the checkout check do run through the recorder first (unlike the refusals other
    # tests assert with a bare `recorder.calls == []`, which happen before even those).
    assert [call for call in recorder.calls if call[0] == "launchctl"] == []


def test_the_arguments_the_registry_tick_template_passes_are_accepted_by_the_module() -> (
    None
):
    """Seam: the template runs the module with --omni-home and --state-dir; the module's parser must accept both."""
    from omnimarket.nodes.node_friction_rollup_tick_effect.runtime_friction_rollup_tick import (
        _parser,
    )

    parsed = _parser().parse_args(
        ["--omni-home", "/registry", "--state-dir", "/evidence"]
    )
    assert parsed.registry_root == "/registry"
    assert parsed.state_dir == "/evidence"
