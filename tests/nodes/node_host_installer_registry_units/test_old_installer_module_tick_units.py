# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_host_installer_registry_units: the rescue-patch-review and consumer-group-guard units are declared launchd registry_module_tick units (OMN-24144)."""

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


@pytest.fixture(autouse=True)
def brew_python(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units.interpreter_present",
        lambda path: path == BREW_PYTHON,
    )
    monkeypatch.setattr(
        shutil, "which", lambda name: "/usr/bin/uv" if name == "uv" else None
    )


UNITS = [
    {
        "unit": "rescue-patch-review",
        "label": "ai.omninode.rescue-patch-review",
        "start_interval": 21600,
        "module": "omnimarket.nodes.node_rescue_patch_review_effect",
        "entry_file": "src/omnimarket/nodes/node_rescue_patch_review_effect/__main__.py",
    },
    {
        "unit": "consumer-group-guard",
        "label": "ai.omninode.consumer-group-guard",
        "start_interval": 300,
        "module": "omnimarket.nodes.node_consumer_group_guard_effect",
        "entry_file": "src/omnimarket/nodes/node_consumer_group_guard_effect/__main__.py",
    },
]


@pytest.mark.parametrize("spec", UNITS, ids=[u["unit"] for u in UNITS])
def test_the_unit_is_declared_on_launchd_with_the_interval_module_and_entry_file(
    spec: dict,
) -> None:
    unit = load_units()[spec["unit"]]
    assert unit["platform"] == "launchd"
    assert unit["kind"] == "registry_module_tick"
    assert unit["label"] == spec["label"]
    assert unit["schedule"]["start_interval"] == spec["start_interval"]
    assert unit["module"] == spec["module"]
    assert unit["entry_file"] == spec["entry_file"]


@pytest.mark.parametrize("spec", UNITS, ids=[u["unit"] for u in UNITS])
@pytest.mark.usefixtures("canonical")
def test_install_renders_the_plist_with_matching_label_interval_runatload_and_module(
    tmp_path: Path, spec: dict
) -> None:
    branch = tmp_path / "branch"
    revision = make_checkout(branch, entry=spec["entry_file"])
    result = HandlerRegistryUnits(Recorder()).handle(
        request(
            tmp_path,
            unit=spec["unit"],
            mode="install",
            project_path=str(branch),
            expected_revision=revision,
        )
    )
    assert result.status == "success", result.stderr
    plist = plist_of(result.rendered)
    assert plist["Label"] == spec["label"]
    assert plist["StartInterval"] == spec["start_interval"]
    assert plist["RunAtLoad"] is True
    args = plist["ProgramArguments"]
    assert args[args.index("-m") + 1] == spec["module"]
    assert str(branch) in args


@pytest.mark.parametrize("spec", UNITS, ids=[u["unit"] for u in UNITS])
@pytest.mark.usefixtures("canonical")
def test_a_checkout_without_the_entry_file_is_refused(
    tmp_path: Path, spec: dict
) -> None:
    branch = tmp_path / "branch"
    make_checkout(branch, entry="src/omnimarket/nodes/other/__main__.py")
    result = HandlerRegistryUnits(Recorder()).handle(
        request(
            tmp_path,
            unit=spec["unit"],
            mode="install",
            project_path=str(branch),
        )
    )
    assert result.status == "failure"


@pytest.mark.parametrize("spec", UNITS, ids=[u["unit"] for u in UNITS])
@pytest.mark.usefixtures("canonical")
def test_dry_run_renders_and_makes_no_launchctl_call(
    tmp_path: Path, spec: dict
) -> None:
    branch = tmp_path / "branch"
    revision = make_checkout(branch, entry=spec["entry_file"])
    recorder = Recorder()
    result = HandlerRegistryUnits(recorder).handle(
        request(
            tmp_path,
            unit=spec["unit"],
            mode="dry-run",
            project_path=str(branch),
            expected_revision=revision,
        )
    )
    assert result.status == "success", result.stderr
    assert not any("launchctl" in call[0] for call in recorder.calls)
