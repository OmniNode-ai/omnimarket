# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_host_installer_registry_units: the overlay store refresh units name the overlays clone before the plugin copy (OMN-23844).

The overlay store reader takes the list of overlays it keeps from the overlay store read under these roots, so the
canonical overlays clone comes first and the plugin's copy is the fallback, the way the factory floor publisher
already declares its roots (OMN-23323).
"""

from __future__ import annotations

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
REFRESH_UNITS = ("overlay-store-refresh", "overlay-store-refresh-systemd")
LOADER_UNIT = "overlay-store-load-systemd"
# The clone first, then the plugin copy, matching the factory floor publisher's declared form.
CLONE_THEN_PLUGIN = "{overlays_root}:{marketplace_root}/plugins/onex-overlays/overlays"


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


@pytest.mark.parametrize("unit", REFRESH_UNITS)
def test_default_refresh_units_name_the_overlays_clone_then_the_plugin(
    unit: str,
) -> None:
    assert load_units()[unit]["environment"][ROOTS] == CLONE_THEN_PLUGIN


def test_same_clone_first_root_for_refresh_and_loader() -> None:
    refresh = load_units()["overlay-store-refresh-systemd"]["environment"][ROOTS].split(
        ":"
    )
    loader = load_units()[LOADER_UNIT]["environment"][ROOTS].split(":")
    # Both keep the overlays canonical clone as their first (primary) root.
    assert refresh[0] == loader[0]


def test_rendered_unit_text_carries_the_clone_before_the_plugin(tmp_path: Path) -> None:
    path = tmp_path / "branch"
    revision = base.make_checkout(path)
    request = base.request(
        tmp_path,
        unit="overlay-store-refresh-systemd",
        mode="install",
        project_path=str(path),
        expected_revision=revision,
        # Only the host name is overridden; the overlay roots keep the unit's new default.
        path_overrides={"LAB_WORK_HOST_NAME": "lab-host"},
        marketplace_root=str(tmp_path / "market"),
    )
    result = HandlerRegistryUnits(base.Recorder()).handle(request)
    assert result.status == "success", result.stderr
    unit_dir = tmp_path / "home" / ".config/systemd/user"
    service = (unit_dir / f"{base.LABEL}.service").read_text(encoding="utf-8")
    clone = f"{tmp_path}/omni/overlays/overlays"
    plugin = f"{tmp_path}/market/plugins/onex-overlays/overlays"
    assert f'"{ROOTS}={clone}:{plugin}"' in service
    assert service.index(clone) < service.index(plugin)


@pytest.mark.parametrize("unit", REFRESH_UNITS)
def test_error_a_worktree_override_is_still_refused_for_the_refresh_units(
    tmp_path: Path,
    unit: str,
) -> None:
    worktree = f"{tmp_path}/omni/omni_worktrees/OMN-23844/overlays/overlays"
    with pytest.raises(UnitRootRefusedError):
        environment_of(tmp_path, unit, path_overrides={ROOTS: worktree})
