# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A registry unit's overlay root is never an omni_worktrees path (OMN-23323).

The floor publisher read its owners map from a worktree installed through the overlay-root path override. Worktree triage
removed the worktree and the published floor lost every owner ticket added that day. The installer now refuses a worktree
path in an overlay-root variable, whether it comes from the unit's default or from a request override, and the factory
floor publisher's default carries the overlays canonical clone instead.
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

pytestmark = pytest.mark.unit

ROOTS = "ONEX_SKILL_OVERLAY_ROOTS"
# Every unit that declares the overlay root as a path override declares the refusal (OMN-23323).
OVERLAY_ROOT_UNITS = (
    "factory-floor-publish",
    "overlay-store-refresh",
    "overlay-store-refresh-systemd",
    "lab-test-harness",
    "job-log-filler",
)


def environment_of(
    tmp_path: Path, unit: str, units: dict[str, Any] | None = None, **fields: Any
) -> dict[str, str]:
    base: dict[str, Any] = {
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
        **{**base, **fields, "path_overrides": overrides}
    )
    handler = HandlerRegistryUnits(
        lambda argv: subprocess.CompletedProcess(argv, 0, "test-user", "")
    )
    return handler.environment(request, declared[unit])


def roots_of(tmp_path: Path, **fields: Any) -> str:
    return environment_of(tmp_path, "factory-floor-publish", **fields)[ROOTS]


def test_golden_the_floor_publisher_default_names_the_overlays_clone_and_the_plugin_and_no_worktree(
    tmp_path: Path,
) -> None:
    roots = roots_of(tmp_path).split(":")
    assert roots == [
        f"{tmp_path}/omni/overlays/overlays",
        f"{tmp_path}/market/plugins/onex-overlays/overlays",
    ]
    assert not any("omni_worktrees" in entry for entry in roots)


def test_golden_an_override_naming_the_overlay_store_is_accepted(
    tmp_path: Path,
) -> None:
    store = f"{tmp_path}/store/overlays/overlays"
    assert roots_of(tmp_path, path_overrides={ROOTS: store}) == store


@pytest.mark.parametrize("unit", OVERLAY_ROOT_UNITS)
def test_every_overlay_root_unit_declares_the_refusal(unit: str) -> None:
    assert ROOTS in load_units()[unit]["environment_refuse_worktree_paths"]


def test_error_an_override_naming_a_worktree_is_refused_with_the_path_named(
    tmp_path: Path,
) -> None:
    worktree = f"{tmp_path}/omni/omni_worktrees/OMN-23081/overlays/overlays"
    with pytest.raises(UnitRootRefusedError) as refused:
        roots_of(tmp_path, path_overrides={ROOTS: worktree})
    assert refused.value.paths == (worktree,)


def test_error_one_worktree_entry_in_a_colon_list_refuses_the_whole_list(
    tmp_path: Path,
) -> None:
    worktree = f"{tmp_path}/omni/omni_worktrees/OMN-23081/overlays/overlays"
    with pytest.raises(UnitRootRefusedError):
        roots_of(
            tmp_path, path_overrides={ROOTS: f"{tmp_path}/market/overlays:{worktree}"}
        )


@pytest.mark.parametrize("unit", OVERLAY_ROOT_UNITS)
def test_error_each_overlay_root_unit_refuses_a_worktree_override(
    tmp_path: Path, unit: str
) -> None:
    worktree = f"{tmp_path}/omni/omni_worktrees/OMN-1/overlays"
    with pytest.raises(UnitRootRefusedError):
        environment_of(tmp_path, unit, path_overrides={ROOTS: worktree})


def test_error_a_registry_root_that_is_a_worktree_makes_the_default_refused(
    tmp_path: Path,
) -> None:
    """The default is checked as well as the override: a default can never name a worktree either."""
    request_root = tmp_path / "omni_worktrees" / "OMN-1" / "registry"
    with pytest.raises(UnitRootRefusedError):
        environment_of(
            tmp_path, "factory-floor-publish", registry_root=str(request_root)
        )


def test_named_break_without_the_declaration_the_worktree_override_is_accepted(
    tmp_path: Path,
) -> None:
    """The failing control: the refusal is the declaration, not an accident of the path."""
    units = load_units()
    units["factory-floor-publish"].pop("environment_refuse_worktree_paths")
    worktree = f"{tmp_path}/omni/omni_worktrees/OMN-23081/overlays/overlays"
    environment = environment_of(
        tmp_path, "factory-floor-publish", units, path_overrides={ROOTS: worktree}
    )
    assert environment[ROOTS] == worktree
