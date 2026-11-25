# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A registry unit never runs a project that is a linked git worktree, wherever it sits (OMN-16782).

The silent-failure alarm unit on a lab host was installed through the project path override from a remote lane's run
directory, ``<lab work root>/runs/rlane-<lane>-<id>``. That directory is a linked worktree of the registry clone, but its
path has no ``omni_worktrees`` segment, so the name-based refusal (OMN-26549) let it through. The lane ended, its run
directory was removed, and ``uv run --project <gone>`` exited 2 on every 300 s fire: the alarm that reports failed
units was itself the failed unit, and its last tick ran seven days earlier. The installer now asks git whether the
project is a linked worktree, which holds for a lane tree wherever the lab places it.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

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

# `linux_host` is taken from the sibling module and applied to every test here; naming it in `__all__` marks it used.
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
                "ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "skill_roots"),
                "LAB_WORK_HOST_NAME": "h053",
            },
            **fields,
        }
    )


def _linked_worktree(tmp_path: Path, name: str) -> Path:
    """A linked worktree of a fresh clone, at a lab-run style path with no ``omni_worktrees`` segment."""
    main = tmp_path / "clone"
    _checkout(main)
    tree = tmp_path / "lab-run" / "runs" / name
    tree.parent.mkdir(parents=True)
    subprocess.run(
        ["git", "-C", str(main), "worktree", "add", "--detach", str(tree), "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        env=scrub_git_location_env(os.environ),
    )
    return tree


def _revision(path: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        env=scrub_git_location_env(os.environ),
    ).stdout.strip()


def test_golden_a_main_checkout_installs(tmp_path: Path) -> None:
    """Positive control: a main checkout (its own git directory) is accepted by the same request."""
    project = tmp_path / "branch"
    revision = _checkout(project)
    result = HandlerRegistryUnits(Recorder()).handle(
        _request(tmp_path, project, expected_revision=revision)
    )
    assert result.status == "success", result.stderr


def test_error_a_linked_worktree_outside_omni_worktrees_is_refused(
    tmp_path: Path,
) -> None:
    project = _linked_worktree(tmp_path, "rlane-silentalarm-0000")
    assert "omni_worktrees" not in project.parts
    result = HandlerRegistryUnits(Recorder()).handle(
        _request(tmp_path, project, expected_revision=_revision(project))
    )
    assert result.status == "failure"
    assert str(project) in result.stderr
    assert "linked worktree" in result.stderr
    assert not (tmp_path / "home" / ".config" / "systemd" / "user").exists(), (
        "a refused install wrote no unit file"
    )


def test_error_a_dry_run_refuses_it_too(tmp_path: Path) -> None:
    project = _linked_worktree(tmp_path, "rlane-silentalarm-0001")
    result = HandlerRegistryUnits(Recorder()).handle(
        _request(
            tmp_path, project, expected_revision=_revision(project), mode="dry-run"
        )
    )
    assert result.status == "failure"
    assert str(project) in result.stderr


def test_every_unit_that_declares_the_override_refuses_a_linked_worktree(
    tmp_path: Path,
) -> None:
    declared = {
        name: unit
        for name, unit in load_units().items()
        if unit.get("project_path_override")
    }
    assert PROBE_UNIT in declared
    project = _linked_worktree(tmp_path, "rlane-silentalarm-0002")
    handler = HandlerRegistryUnits(Recorder())
    for name, unit in declared.items():
        request = ModelRegistryUnitsRequest.model_validate(
            {
                "unit": name,
                "home": str(tmp_path / "home"),
                "registry_root": str(tmp_path / "omni"),
                "project_path": str(project),
            }
        )
        with pytest.raises(ValueError, match="linked worktree") as refused:
            handler.project_for(request, unit)
        assert str(project) in str(refused.value), name
