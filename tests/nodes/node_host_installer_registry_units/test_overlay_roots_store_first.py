# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_host_installer_registry_units: every unit that names overlay roots lists the overlays clone first (OMN-23863).

The overlay a unit reads under its ONEX_SKILL_OVERLAY_ROOTS lives in the overlays canonical clone, so the clone
comes first and the plugin's copy is the fallback, the form overlay-store-refresh-systemd and the factory floor
publisher already declare.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from omnimarket.handlers.rules_unit_roots import UnitRootRefusedError
from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    HandlerRegistryUnits,
    load_units,
)
from omnimarket.nodes.node_host_installer_registry_units.models.model_registry_units import (
    ModelRegistryUnitsRequest,
)
from tests.nodes.node_host_installer_registry_units import (
    test_registry_units_systemd as base,
)

pytestmark = pytest.mark.unit

ROOTS = "ONEX_SKILL_OVERLAY_ROOTS"
CLONE = "{registry_root}/overlays/overlays"
PLUGIN = "{marketplace_root}/plugins/onex-overlays/overlays"
# The clone first, then the plugin copy, matching the form the factory floor publisher declares.
CLONE_THEN_PLUGIN = f"{CLONE}{os.pathsep}{PLUGIN}"
CHANGED_UNITS = (
    "lane-ctx-ledger-dedupe",
    "c32-auth-boundary-probe",
    "lab-test-harness",
    "job-log-filler",
    "landing-join-j8",
)


@pytest.fixture(autouse=True)
def linux_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        f"{base.HANDLER}.interpreter_present", lambda path: path == base.LINUX_PYTHON
    )
    monkeypatch.setattr(
        "shutil.which", lambda name: "/usr/bin/uv" if name == "uv" else None
    )


def environment_of(
    tmp_path: Path, unit: str, units: dict[str, Any] | None = None, **fields: Any
) -> dict[str, str]:
    request_fields: dict[str, Any] = {
        "unit": unit,
        "home": str(tmp_path / "home"),
        "registry_root": str(tmp_path / "omni"),
        "workspace_config_root": str(tmp_path / "ws"),
        "plans_path": str(tmp_path / "plans"),
        "marketplace_root": str(tmp_path / "market"),
        "runtime_ssh_host": "lab-runtime",
        "ledger_host_name": "lab-ledger-host",
    }
    declared = units or load_units()
    overrides = dict(fields.pop("path_overrides", {}))
    if "LAB_WORK_HOST_NAME" in declared[unit].get("environment_path_overrides", []):
        overrides.setdefault(
            "LAB_WORK_HOST_NAME", "lab-host"
        )  # the unit's own placeholder
    request = ModelRegistryUnitsRequest(
        **{**request_fields, **fields, "path_overrides": overrides}
    )
    handler = HandlerRegistryUnits(
        lambda argv: subprocess.CompletedProcess(argv, 0, "test-user", "")
    )
    return handler.environment(request, declared[unit])


def clone_first_offenders(units: dict[str, Any]) -> list[str]:
    """The names of the units whose overlay roots do not list the overlays clone first."""
    return [
        name
        for name, unit in units.items()
        if ROOTS in (unit.get("environment") or {})
        and str(unit["environment"][ROOTS]).split(os.pathsep)[0] != CLONE
    ]


def test_every_unit_that_declares_overlay_roots_names_the_clone_first() -> None:
    offenders = clone_first_offenders(load_units())
    assert offenders == [], (
        f"these units name an overlay root before the overlays clone: {', '.join(offenders)}"
    )


@pytest.mark.parametrize("unit", CHANGED_UNITS)
def test_the_five_changed_units_name_the_clone_then_the_plugin(unit: str) -> None:
    assert load_units()[unit]["environment"][ROOTS] == CLONE_THEN_PLUGIN


@pytest.mark.parametrize("unit", CHANGED_UNITS)
def test_golden_rendered_environment_carries_the_clone_before_the_plugin(
    tmp_path: Path,
    unit: str,
) -> None:
    roots = environment_of(tmp_path, unit)[ROOTS].split(os.pathsep)
    assert roots == [
        f"{tmp_path}/omni/overlays/overlays",
        f"{tmp_path}/market/plugins/onex-overlays/overlays",
    ]
    assert not any("omni_worktrees" in entry for entry in roots)


@pytest.mark.parametrize("unit", CHANGED_UNITS)
def test_error_a_worktree_override_is_refused_for_each_changed_unit(
    tmp_path: Path,
    unit: str,
) -> None:
    worktree = f"{tmp_path}/omni/omni_worktrees/OMN-23863/overlays/overlays"
    with pytest.raises(UnitRootRefusedError):
        environment_of(tmp_path, unit, path_overrides={ROOTS: worktree})


def test_control_the_check_names_a_unit_that_lists_the_plugin_first() -> None:
    units = load_units()
    unit = dict(units["lab-test-harness"])
    environment = dict(unit["environment"])
    environment[ROOTS] = PLUGIN
    unit["environment"] = environment
    units["lab-test-harness"] = unit
    assert clone_first_offenders(units) == ["lab-test-harness"]
