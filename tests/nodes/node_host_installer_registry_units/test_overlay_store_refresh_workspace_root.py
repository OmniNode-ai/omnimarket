# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay store refresh launchd job carries the workspace root the store locates its bus lane under (OMN-24137).

launchd does not inherit the installing shell's environment, so the job reads the workspace root from the plist the
installer wrote and nowhere else. The unit's declaration already names ``OMNIBASE_PATH`` (OMN-24052); this test checks
the file the Mac runs, rendered through the installer as it would write it: the value is the registry root, never the
job root the unit stands in (``launchd_root``, which is also what the tick receives as its omni home), and the overlay
store's workspace reader finds it. The job failed every run for days on the installed copy of a declaration that
lacked it, and the Mac's copy of ``lab_hosts`` went stale.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from omnimarket.handlers.overlay_store.bus_opener import workspace_root
from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    HandlerRegistryUnits,
    load_units,
)
from tests.nodes.node_host_installer_registry_units.test_log_directory import (
    BREW_PYTHON,
    READER,
    READER_ENTRY,
)
from tests.nodes.node_host_installer_registry_units.test_registry_units import (
    make_checkout,
    plist_of,
    real_run,
    request,
)

pytestmark = pytest.mark.unit

WORKSPACE = "OMNIBASE_PATH"


@pytest.fixture(autouse=True)
def brew_python(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units.interpreter_present",
        lambda path: path == BREW_PYTHON,
    )


def render(
    tmp_path: Path, units: dict[str, dict[str, Any]] | None = None
) -> dict[str, Any]:
    """The plist the installer writes for the Mac refresh job, as an install over a branch checkout renders it."""
    branch = tmp_path / "branch"
    revision = make_checkout(branch, entry=READER_ENTRY)
    result = HandlerRegistryUnits(real_run, units=units).handle(
        request(
            tmp_path,
            unit=READER,
            project_path=str(branch),
            expected_revision=revision,
            path_overrides={
                "ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "overlays"),
                "LAB_WORK_HOST_NAME": "h101",
            },
        )
    )
    assert result.status == "success", result.stderr
    return plist_of(result.rendered)


def test_the_unit_declares_the_registry_root_as_the_workspace_root() -> None:
    declared = load_units()[READER]
    assert declared["platform"] == "launchd"
    assert declared["environment"][WORKSPACE] == "{registry_root}"


def test_golden_the_rendered_job_carries_the_registry_root_not_its_job_root(
    tmp_path: Path,
) -> None:
    plist = render(tmp_path)
    environment = plist["EnvironmentVariables"]
    job_root = str(tmp_path / "home/.local/state/omni/overlay-store-refresh")
    # Positive control: the job does stand in a directory that is not the registry root.
    assert plist["WorkingDirectory"] == job_root
    assert environment["OMNI_HOME"] == job_root
    assert environment[WORKSPACE] == str(tmp_path / "omni")
    assert environment[WORKSPACE] != job_root


def test_golden_the_overlay_stores_workspace_reader_finds_the_root_in_the_rendered_job(
    tmp_path: Path,
) -> None:
    environment = render(tmp_path)["EnvironmentVariables"]
    assert workspace_root(environment) == tmp_path / "omni"


def test_error_a_job_rendered_without_the_setting_gives_the_reader_no_workspace_root(
    tmp_path: Path,
) -> None:
    """Failing control: the declaration with OMNIBASE_PATH removed renders a job the reader finds no root in."""
    units = copy.deepcopy(load_units())
    del units[READER]["environment"][WORKSPACE]
    environment = render(tmp_path, units)["EnvironmentVariables"]
    assert WORKSPACE not in environment
    assert workspace_root(environment) is None
