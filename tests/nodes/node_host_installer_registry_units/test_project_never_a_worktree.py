# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A registry unit never runs a project that sits under omni_worktrees (OMN-26549).

The overlay env probe unit was installed from a lane's worktree through the project path override. The worktree was
removed the same day, ``uv run --project <gone>`` exited 2 on every 900 s fire and the unit stayed failed until it was
read. The other installers already refuse a unit that names an ``omni_worktrees`` path before they write it
(``refuse_worktree_roots``); this installer refused one only in an overlay-root variable. It now refuses it in the
project too, for every unit that declares the override.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.handlers.rules_unit_roots import UnitRootRefusedError
from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    HandlerRegistryUnits,
    load_units,
)
from omnimarket.nodes.node_host_installer_registry_units.models.model_registry_units import (
    ModelRegistryUnitsRequest,
)
from tests.nodes.node_host_installer_registry_units.test_overlay_env_probe_unit import (
    _checkout,
    linux_host,
)
from tests.nodes.node_host_installer_registry_units.test_registry_units_systemd import (
    Recorder,
)

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("linux_host")]

# `linux_host` (a Linux host with uv) is taken from the sibling module and applied to every test here, so the
# result does not depend on which interpreters the host has; naming it in `__all__` marks the import as used.
__all__ = ["linux_host"]

PROBE_UNIT = "overlay-env-probe-systemd"


def _request(
    tmp_path: Path, project: Path, **fields: object
) -> ModelRegistryUnitsRequest:
    return ModelRegistryUnitsRequest.model_validate(
        {
            "unit": PROBE_UNIT,
            "home": str(tmp_path / "home"),
            "registry_root": str(tmp_path / "omni"),
            "project_path": str(project),
            "path_overrides": {
                "ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "overlays" / "overlays"),
                "LAB_WORK_HOST_NAME": "h053",
            },
            **fields,
        }
    )


def test_golden_a_checkout_outside_omni_worktrees_installs(
    tmp_path: Path,
) -> None:
    """Positive control: the same checkout, not under omni_worktrees, is accepted by the same request."""
    project = tmp_path / "branch"
    revision = _checkout(project)
    result = HandlerRegistryUnits(Recorder()).handle(
        _request(tmp_path, project, expected_revision=revision)
    )
    assert result.status == "success", result.stderr


def test_error_a_checkout_under_omni_worktrees_is_refused_with_the_path_named(
    tmp_path: Path,
) -> None:
    project = tmp_path / "omni" / "omni_worktrees" / "lane-1" / "omnimarket"
    revision = _checkout(project)
    result = HandlerRegistryUnits(Recorder()).handle(
        _request(tmp_path, project, expected_revision=revision)
    )
    assert result.status == "failure"
    assert str(project) in result.stderr
    assert "omni_worktrees" in result.stderr
    assert not (tmp_path / "home" / ".config" / "systemd" / "user").exists(), (
        "a refused install wrote no unit file"
    )


def test_error_a_dry_run_refuses_it_too(
    tmp_path: Path,
) -> None:
    project = tmp_path / "omni" / "omni_worktrees" / "lane-1" / "omnimarket"
    revision = _checkout(project)
    result = HandlerRegistryUnits(Recorder()).handle(
        _request(tmp_path, project, expected_revision=revision, mode="dry-run")
    )
    assert result.status == "failure"
    assert str(project) in result.stderr


def test_every_unit_that_declares_the_override_refuses_a_worktree_project(
    tmp_path: Path,
) -> None:
    declared = {
        name: unit
        for name, unit in load_units().items()
        if unit.get("project_path_override")
    }
    assert PROBE_UNIT in declared
    handler = HandlerRegistryUnits(Recorder())
    project = tmp_path / "omni" / "omni_worktrees" / "lane-1" / "omnimarket"
    for name, unit in declared.items():
        request = ModelRegistryUnitsRequest.model_validate(
            {
                "unit": name,
                "home": str(tmp_path / "home"),
                "registry_root": str(tmp_path / "omni"),
                "project_path": str(project),
            }
        )
        with pytest.raises(UnitRootRefusedError) as refused:
            handler.project_for(request, unit)
        assert refused.value.paths == (str(project),), name
