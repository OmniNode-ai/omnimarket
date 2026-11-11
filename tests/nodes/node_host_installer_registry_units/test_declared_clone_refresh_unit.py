# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_host_installer_registry_units: the declared-clone refresh tick is a declared launchd unit on the lab host (OMN-23557). One tick refreshes the clone this host's lab_hosts row declares for the knowledge base, which the registry-root scan cannot list; the overlay of node_canonical_clone_refresh_effect holds every value the unit does not declare."""

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


@pytest.fixture
def canonical(tmp_path: Path) -> Path:
    path = tmp_path / "omni" / "omnimarket"
    make_checkout(path)
    return path


UNIT = "declared-clone-refresh"
ENTRY = "src/omnimarket/nodes/node_canonical_clone_refresh_effect/runtime_declared_clone_tick.py"


@pytest.fixture(autouse=True)
def brew_python(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units.interpreter_present",
        lambda path: path == BREW_PYTHON,
    )
    monkeypatch.setattr(
        shutil, "which", lambda name: "/usr/bin/uv" if name == "uv" else None
    )


def test_the_unit_is_declared_every_300_s_on_launchd_and_runs_the_tick_module() -> None:
    unit = load_units()[UNIT]
    assert unit["platform"] == "launchd"
    assert unit["schedule"]["start_interval"] == 300
    assert unit["run_timeout_s"] < 300
    assert (
        unit["module"]
        == "omnimarket.nodes.node_canonical_clone_refresh_effect.runtime_declared_clone_tick"
    )
    assert unit["entry_file"] == ENTRY
    assert unit["environment"]["LAB_WORK_HOST_NAME"] == "{host_name}"


@pytest.mark.usefixtures("canonical")
def test_install_renders_the_unit_running_the_tick_from_the_branch_checkout(
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
            path_overrides={"LAB_WORK_HOST_NAME": "h101"},
        )
    )
    assert result.status == "success", result.stderr
    plist = plist_of(result.rendered)
    assert plist["Label"] == "ai.omninode.declared-clone-refresh"
    assert plist["StartInterval"] == 300
    assert plist["RunAtLoad"] is True
    args = plist["ProgramArguments"]
    assert (
        args[args.index("-m") + 1]
        == "omnimarket.nodes.node_canonical_clone_refresh_effect.runtime_declared_clone_tick"
    )
    assert str(branch) in args
    env = plist["EnvironmentVariables"]
    assert env["LAB_WORK_HOST_NAME"] == "h101"


@pytest.mark.usefixtures("canonical")
def test_a_checkout_without_the_tick_entry_file_is_refused(tmp_path: Path) -> None:
    branch = tmp_path / "branch"
    make_checkout(
        branch, entry="src/omnimarket/nodes/other/runtime_declared_clone_tick.py"
    )
    result = HandlerRegistryUnits(Recorder()).handle(
        request(
            tmp_path,
            unit=UNIT,
            mode="install",
            project_path=str(branch),
        )
    )
    assert result.status == "failure"


def test_the_units_entry_file_exists_in_this_repository() -> None:
    unit = load_units()[UNIT]
    assert (Path(__file__).parents[3] / unit["entry_file"]).is_file()
