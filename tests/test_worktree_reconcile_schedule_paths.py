# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A Mac host's layout differs: the overlay resolves its paths per host (OMN-22799).

A Mac's registry root is a home directory path an overlay file may never carry, so the
overlay writes ``$OMNI_HOME/...`` and the loader expands it from the installing process's
environment.
"""

import json
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.events.worktree_reconcile import ModelWorktreeReconcileCommand
from omnimarket.nodes.node_worktree_reconcile_effect.handlers.handler_worktree_schedule import (
    HandlerWorktreeSchedule,
    load_overlay,
)
from omnimarket.nodes.node_worktree_reconcile_effect.models.model_worktree_schedule import (
    ModelWorktreeScheduleOverlay,
    ModelWorktreeScheduleRequest,
)
from tests.test_worktree_reconcile_schedule import overlay

pytestmark = pytest.mark.unit


def second_project(tmp_path: Path) -> Path:
    """A second checkout a host path override can name; the fixture's project stays global."""
    project = tmp_path / "mac_project"
    entry = project / "src/omnimarket/nodes/node_worktree_reconcile_effect/__main__.py"
    entry.parent.mkdir(parents=True)
    entry.write_text(
        (
            Path(__file__).resolve().parents[1]
            / "src/omnimarket/nodes/node_worktree_reconcile_effect/__main__.py"
        ).read_text()
    )
    for argv in (
        ["git", "init", "--quiet", str(project)],
        ["git", "-C", str(project), "add", "src"],
        [
            "git",
            "-C",
            str(project),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "fixture",
        ],
    ):
        subprocess.run(argv, check=True, capture_output=True, text=True)
    return project


def two_host_overlay(tmp_path: Path, host_paths: dict | None = None) -> Path:
    """The fixture overlay with a second host, and optionally that host's path overrides."""
    path = overlay(tmp_path)
    document = yaml.safe_load(path.read_text())
    document["hosts"]["mac-host"] = json.loads(
        ModelWorktreeReconcileCommand(
            host="mac-host",
            roots=(str(tmp_path / "mac" / "registry" / "trees"),),
            correlation_id=uuid4(),
        ).model_dump_json()
    )
    if host_paths is not None:
        document["host_paths"] = {"mac-host": host_paths}
    path.write_text(yaml.safe_dump(document))
    return path


def render(tmp_path: Path, path: Path, host: str) -> str:
    result = HandlerWorktreeSchedule(unit_directory=tmp_path / "units").handle(
        ModelWorktreeScheduleRequest(mode="render", host=host, overlay_path=str(path))
    )
    assert result.exit_code == 0, result.stderr
    return result.service


def test_a_host_path_override_wins_and_another_host_keeps_the_globals(
    tmp_path: Path,
) -> None:
    mac_project = second_project(tmp_path)
    global_project = tmp_path / "project"
    path = two_host_overlay(tmp_path, host_paths={"project": str(mac_project)})
    # The host with the override renders its own project; the other host the globals.
    assert f"WorkingDirectory={mac_project}" in render(tmp_path, path, "mac-host")
    lab_service = render(tmp_path, path, "lab-host")
    assert f"WorkingDirectory={global_project}" in lab_service
    assert f"PYTHONPATH={global_project}/src" in lab_service
    # Failing control: with the override removed the same host names the global project.
    document = yaml.safe_load(path.read_text())
    del document["host_paths"]
    control = tmp_path / "control.yaml"
    control.write_text(yaml.safe_dump(document))
    assert f"WorkingDirectory={global_project}" in render(tmp_path, control, "mac-host")


def test_omni_home_expands_in_roots_ledger_and_a_host_path_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The overlay load path of every scheduled tick, with the environment variable set."""
    home = tmp_path / "home"
    monkeypatch.setenv("OMNI_HOME", f"{home}/")
    document = yaml.safe_load(overlay(tmp_path).read_text())
    document["python"] = "$OMNI_HOME/bin/python"
    document["hosts"]["lab-host"]["roots"] = ["$OMNI_HOME/registry/trees"]
    document["hosts"]["lab-host"]["ledger_path"] = "$OMNI_HOME/state/ledger.jsonl"
    document["hosts"]["lab-host"]["merged_pr_command"] = [
        "$OMNI_HOME/bin/tool",
        "{slug}",
    ]
    document["host_paths"] = {"lab-host": {"state_directory": "$OMNI_HOME/state"}}
    path = tmp_path / "expanding.yaml"
    path.write_text(yaml.safe_dump(document))
    loaded = load_overlay(str(path), "lab-host")
    # The one trailing slash of the variable is stripped, so the joins stay clean.
    assert loaded.python == f"{home}/bin/python"
    assert loaded.state_directory == f"{home}/state"
    assert loaded.project == str(tmp_path / "project")
    command = loaded.hosts["lab-host"]
    assert command.roots == (f"{home}/registry/trees",)
    assert command.ledger_path == f"{home}/state/ledger.jsonl"
    # A command argv keeps its $OMNI_HOME: the shell expands it at run time.
    assert command.merged_pr_command == ["$OMNI_HOME/bin/tool", "{slug}"]


def test_an_unset_omni_home_refuses_the_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OMNI_HOME", raising=False)
    document = yaml.safe_load(overlay(tmp_path).read_text())
    document["project"] = "$OMNI_HOME/project"
    path = tmp_path / "unset.yaml"
    path.write_text(yaml.safe_dump(document))
    result = HandlerWorktreeSchedule(unit_directory=tmp_path / "units").handle(
        ModelWorktreeScheduleRequest(
            mode="render", host="lab-host", overlay_path=str(path)
        )
    )
    assert result.exit_code == 1
    assert "OMNI_HOME is not set" in result.stderr


def test_a_host_paths_key_for_an_unknown_host_is_rejected(tmp_path: Path) -> None:
    document = yaml.safe_load(overlay(tmp_path).read_text())
    document["host_paths"] = {"ghost-host": {"project": str(tmp_path / "elsewhere")}}
    with pytest.raises(ValidationError):
        ModelWorktreeScheduleOverlay.model_validate(document)


def test_an_overlay_without_host_paths_resolves_to_the_globals(tmp_path: Path) -> None:
    """The existing overlays carry no host_paths: every host keeps the four globals."""
    parsed = ModelWorktreeScheduleOverlay.model_validate(
        yaml.safe_load(overlay(tmp_path).read_text())
    )
    resolved = parsed.for_host("lab-host")
    assert resolved.python == parsed.python
    assert resolved.project == parsed.project
    assert resolved.state_directory == parsed.state_directory
    assert resolved.environment_file == parsed.environment_file
