# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: node_host_installer_registry_units renders a unit that runs a branch checkout, through the node entry point."""

from __future__ import annotations

import io
import os
import plistlib
import subprocess
from pathlib import Path

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.nodes.node_host_installer_registry_units.__main__ import main
from omnimarket.nodes.node_host_installer_registry_units.models.model_registry_units import (
    ModelRegistryUnitsRequest,
)

ENTRY = "src/omnimarket/nodes/node_factory_floor_publish_effect/__main__.py"


def _git(path: Path, *args: str) -> str:
    done = subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.com",
            *args,
        ],
        capture_output=True,
        text=True,
        check=True,
        env=scrub_git_location_env(os.environ),
    )
    return done.stdout.strip()


def test_the_dry_run_renders_the_branch_checkout_for_the_unit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # the host under test has the brew interpreter (hosted CI is not a Mac)
    monkeypatch.setattr(
        "omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units.interpreter_present",
        lambda path: path == "/opt/homebrew/bin/python3.13",
    )
    branch = tmp_path / "branch"
    (branch / ENTRY).parent.mkdir(parents=True)
    (branch / ENTRY).write_text("print('tick')\n", encoding="utf-8")
    _git(branch, "init", "-q", "-b", "fix")
    _git(branch, "add", "-A")
    _git(branch, "commit", "-q", "-m", "tick")
    revision = _git(branch, "rev-parse", "HEAD")
    request = ModelRegistryUnitsRequest(
        unit="factory-floor-publish",
        home=str(tmp_path / "home"),
        registry_root=str(tmp_path / "root"),
        workspace_config_root=str(tmp_path / "ws"),
        plans_path=str(tmp_path / "plans"),
        marketplace_root=str(tmp_path / "market"),
        project_path=str(branch),
        expected_revision=revision,
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(request.model_dump_json()))
    assert main() == 0
    from omnimarket.nodes.node_host_installer_registry_units.models.model_registry_units import (
        ModelRegistryUnitsResult,
    )

    result = ModelRegistryUnitsResult.model_validate_json(capsys.readouterr().out)
    assert result.status == "success", result.stderr
    arguments = plistlib.loads(result.rendered.encode("utf-8"))["ProgramArguments"]
    assert arguments[arguments.index("--project") + 1] == str(branch)
    assert result.revision == revision
