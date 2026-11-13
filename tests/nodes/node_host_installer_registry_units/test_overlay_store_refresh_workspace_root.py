# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay store reader's tick locates its lane declaration under the registry root, not its job root (OMN-24137).

The store read names the bus lane in the overlay it reads and opens that lane's bus with the workspace root it reads
from ``$OMNIBASE_PATH`` alone. launchd does not inherit the installing shell's environment, so the rendered unit must
carry the registry root itself: the reader job stands in its job root under the home (its ``OMNI_HOME``), which is
not a workspace root.
"""

from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path

import pytest

from omnimarket.handlers.overlay_store.bus_opener import workspace_root
from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    HandlerRegistryUnits,
    load_units,
)
from omnimarket.nodes.node_host_installer_registry_units.models.model_registry_units import (
    ModelRegistryUnitsRequest,
)
from tests.nodes.node_host_installer_registry_units.test_log_directory import (
    BREW_PYTHON,
    READER,
    READER_ENTRY,
    render,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def brew_python(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units.interpreter_present",
        lambda path: path == BREW_PYTHON,
    )


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
