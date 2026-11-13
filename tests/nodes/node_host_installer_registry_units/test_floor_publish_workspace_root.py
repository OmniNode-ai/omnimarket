# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The floor publish timer carries the workspace root its emission mirror locates the lane declaration under (OMN-23877).

The floor_emissions step mirrors each chain event onto the bus, and the mirror opens its lane's bus with the workspace
root it reads from ``$OMNIBASE_PATH`` alone. launchd does not inherit the installing shell's environment, so a unit that
does not persist it refused every mirror with "cannot locate the lane declaration: no workspace root is set" and the
step failed each cycle (measured 2026-11-12 on the launching Mac, integration pass 7).
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

pytestmark = pytest.mark.unit

UNIT = "factory-floor-publish"


def rendered_environment(tmp_path: Path) -> dict[str, str]:
    registry_root = tmp_path / "registry"
    request = ModelRegistryUnitsRequest(
        unit=UNIT,
        home=str(tmp_path),
        registry_root=str(registry_root),
        workspace_config_root=str(tmp_path / "workspace-config"),
        plans_path=str(tmp_path),
        marketplace_root=str(tmp_path),
        runtime_ssh_host="lab-runtime",
        ledger_host_name="lab-host",
    )
    handler = HandlerRegistryUnits(
        lambda argv: subprocess.CompletedProcess(argv, 0, "test-user", "")
    )
    text = handler.render(request, load_units()[UNIT], tmp_path, "uv", "python")
    environment: dict[str, str] = plistlib.loads(text.encode())["EnvironmentVariables"]
    return environment


def test_golden_the_rendered_unit_carries_the_registry_root_as_omnibase_path(
    tmp_path: Path,
) -> None:
    environment = rendered_environment(tmp_path)
    assert environment["OMNIBASE_PATH"] == str(tmp_path / "registry")


def test_golden_the_emission_mirror_reads_a_workspace_root_from_the_rendered_unit(
    tmp_path: Path,
) -> None:
    assert workspace_root(rendered_environment(tmp_path)) == tmp_path / "registry"


def test_error_the_workspace_config_root_alone_gives_the_mirror_no_workspace_root() -> (
    None
):
    assert workspace_root({"ONEX_WORKSPACE_CONFIG_ROOT": "/workspace-config"}) is None
