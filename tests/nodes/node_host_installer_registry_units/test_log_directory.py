# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22878: a registry tick unit may keep its launchd logs off the registry root.

h101's registry root links to an external volume; launchd opens the log files before the job runs and ended the
overlay store reader with exit 78 (EX_CONFIG) and no log. The unit declares log_directory, relative to the home.
"""

from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path
from typing import Any

import pytest

from omnimarket.handlers.overlay_store.bus_opener import workspace_root
from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    HandlerRegistryUnits,
    load_units,
)
from omnimarket.nodes.node_host_installer_registry_units.models.model_registry_units import (
    ModelRegistryUnitsRequest,
)
from tests.nodes.node_host_installer_registry_units.test_registry_units import (
    BREW_PYTHON,
    UNIT,
    make_checkout,
    plist_of,
    real_run,
    request,
)

READER = "overlay-store-refresh"
READER_ENTRY = (
    "src/omnimarket/nodes/node_overlay_store_read_effect/runtime_overlay_store_tick.py"
)


@pytest.fixture(autouse=True)
def brew_python(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units.interpreter_present",
        lambda path: path == BREW_PYTHON,
    )


def render(tmp_path: Path, unit: str, entry: str | None, **kw: Any) -> dict[str, Any]:
    branch = tmp_path / "branch"
    revision = make_checkout(branch, entry=entry) if entry else make_checkout(branch)
    result = HandlerRegistryUnits(real_run).handle(
        request(
            tmp_path,
            unit=unit,
            project_path=str(branch),
            expected_revision=revision,
            **kw,
        )
    )
    assert result.status == "success", result.stderr
    return plist_of(result.rendered)


def test_the_reader_unit_declares_a_log_directory() -> None:
    assert (
        load_units()[READER]["log_directory"]
        == ".local/state/omni/overlay-store-refresh"
    )


def test_golden_the_reader_logs_sit_under_the_home_not_the_registry_root(
    tmp_path: Path,
) -> None:
    plist = render(
        tmp_path,
        READER,
        READER_ENTRY,
        path_overrides={
            "ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "overlays"),
            "LAB_WORK_HOST_NAME": "h101",
        },
    )
    logs = tmp_path / "home/.local/state/omni/overlay-store-refresh"
    assert plist["StandardOutPath"] == str(logs / f"{READER}.out.log")
    assert plist["StandardErrorPath"] == str(logs / f"{READER}.err.log")
    assert str(tmp_path / "omni") not in plist["StandardOutPath"]


def test_error_a_unit_that_declares_no_log_directory_keeps_its_logs_in_the_state_directory(
    tmp_path: Path,
) -> None:
    """Failing control: the floor publisher declares none, so its logs stay where they were."""
    assert "log_directory" not in load_units()[UNIT]
    plist = render(tmp_path, UNIT, None)
    state = tmp_path / "omni" / str(load_units()[UNIT]["state_directory"])
    assert plist["StandardOutPath"] == str(state / f"{UNIT}.out.log")


def test_the_reader_unit_declares_a_launchd_root() -> None:
    assert (
        load_units()[READER]["launchd_root"]
        == ".local/state/omni/overlay-store-refresh"
    )


def test_golden_the_reader_job_runs_from_the_home_not_the_registry_root(
    tmp_path: Path,
) -> None:
    """launchd holds no grant to the external volume: a uv started with a cwd there waits in getcwd forever (h101)."""
    plist = render(
        tmp_path,
        READER,
        READER_ENTRY,
        path_overrides={
            "ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "overlays"),
            "LAB_WORK_HOST_NAME": "h101",
        },
    )
    root = str(tmp_path / "home/.local/state/omni/overlay-store-refresh")
    arguments = plist["ProgramArguments"]
    assert plist["WorkingDirectory"] == root
    assert plist["EnvironmentVariables"]["OMNI_HOME"] == root
    assert arguments[arguments.index("--omni-home") + 1] == root
    assert str(tmp_path / "omni") not in str(plist["WorkingDirectory"])


def test_error_a_unit_that_declares_no_launchd_root_keeps_the_registry_root(
    tmp_path: Path,
) -> None:
    """Failing control: the floor publisher declares none, so its working directory stays the registry root."""
    assert "launchd_root" not in load_units()[UNIT]
    plist = render(tmp_path, UNIT, None)
    assert plist["WorkingDirectory"] == str(tmp_path / "omni")
    assert plist["EnvironmentVariables"]["OMNI_HOME"] == str(tmp_path / "omni")


# OMN-24137: the rendered reader unit carries the registry root as OMNIBASE_PATH.
def reader_environment(tmp_path: Path) -> dict[str, str]:
    plist = render(
        tmp_path,
        READER,
        READER_ENTRY,
        path_overrides={
            "ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "overlays"),
            "LAB_WORK_HOST_NAME": "h101",
        },
    )
    environment: dict[str, str] = plist["EnvironmentVariables"]
    return environment


def test_golden_the_reader_carries_the_registry_root_as_omnibase_path_not_its_job_root(
    tmp_path: Path,
) -> None:
    environment = reader_environment(tmp_path)
    job_root = str(tmp_path / "home/.local/state/omni/overlay-store-refresh")
    assert environment["OMNIBASE_PATH"] == str(tmp_path / "omni")
    assert "ONEX_WORKSPACE_CONFIG_ROOT" in environment
    assert environment["OMNI_HOME"] == job_root
    assert environment["OMNIBASE_PATH"] != job_root


def test_golden_the_store_read_reads_a_workspace_root_from_the_rendered_unit(
    tmp_path: Path,
) -> None:
    assert workspace_root(reader_environment(tmp_path)) == tmp_path / "omni"


def test_error_an_environment_without_omnibase_path_gives_the_store_read_no_workspace_root(
    tmp_path: Path,
) -> None:
    """Failing control: the workspace config root alone is not a workspace root."""
    assert workspace_root({"ONEX_WORKSPACE_CONFIG_ROOT": "/workspace-config"}) is None
    declared = dict(load_units()[READER])
    declared["environment"] = {
        name: template
        for name, template in declared["environment"].items()
        if name != "OMNIBASE_PATH"
    }
    handler = HandlerRegistryUnits(
        lambda argv: subprocess.CompletedProcess(argv, 0, "test-user", "")
    )
    request = ModelRegistryUnitsRequest(
        unit=READER,
        home=str(tmp_path / "home"),
        registry_root=str(tmp_path / "omni"),
        workspace_config_root=str(tmp_path / "ws"),
        plans_path=str(tmp_path / "plans"),
        marketplace_root=str(tmp_path / "market"),
        runtime_ssh_host="lab-runtime",
        ledger_host_name="lab-host",
        path_overrides={
            "ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "overlays"),
            "LAB_WORK_HOST_NAME": "h101",
        },
    )
    text = handler.render(request, declared, tmp_path, "uv", "python")
    environment: dict[str, str] = plistlib.loads(text.encode())["EnvironmentVariables"]
    assert "OMNIBASE_PATH" not in environment
    assert "ONEX_WORKSPACE_CONFIG_ROOT" in environment
    assert workspace_root(environment) is None
