# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""`onex setup` uses the folder it runs from as the workspace (OMN-23413 AC19).

Failure modes:
  F1. Run from a project folder without --workspace, setup ignores that folder
      and uses an environment variable, a recorded root or a prompt instead.
  F2. --workspace no longer wins over the current folder.
  F3. Run from the home folder or the filesystem root, setup turns that whole
      folder into a workspace instead of falling back to the previous chain.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from omnimarket.nodes.node_onex_setup_effect import cli as setup_cli

pytestmark = pytest.mark.unit

_ARGS = ["--no-interactive", "--yes", "--provider", "ollama", "--model", "m"]


def _run(
    monkeypatch: pytest.MonkeyPatch, home: Path, cwd: Path, *extra: str
) -> tuple[Any, list[Path | None]]:
    """Invoke the real setup command; capture the workspace it hands on."""
    seen: list[Path | None] = []

    def capture(ctx: object, **kwargs: Any) -> None:
        seen.append(kwargs["workspace"])

    monkeypatch.setattr(setup_cli, "_workflow_cli", capture)
    monkeypatch.setattr(
        setup_cli,
        "_ask_choices",
        lambda provider, model, url, **_: (provider, model, url),
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("OMNI_HOME", raising=False)
    monkeypatch.chdir(cwd)
    result = CliRunner().invoke(
        setup_cli.setup_command, [*_ARGS, "--url", "http://127.0.0.1:9", *extra]
    )
    return result, seen


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv("OMNIBASE_PATH", raising=False)
    folder = tmp_path / "home"
    folder.mkdir()
    return folder


def test_the_current_folder_is_the_workspace(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F1."""
    project = tmp_path / "project"
    project.mkdir()
    result, seen = _run(monkeypatch, home, project)
    assert result.exit_code == 0, result.output
    assert seen == [project.resolve()]


def test_the_current_folder_wins_over_omnibase_path(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F1: an exported OMNIBASE_PATH no longer outranks the folder you are in."""
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("OMNIBASE_PATH", str(tmp_path / "elsewhere"))
    result, seen = _run(monkeypatch, home, project)
    assert result.exit_code == 0, result.output
    assert seen == [project.resolve()]


def test_workspace_flag_always_wins(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F2."""
    project = tmp_path / "project"
    project.mkdir()
    chosen = tmp_path / "chosen"
    result, seen = _run(monkeypatch, home, project, "--workspace", str(chosen))
    assert result.exit_code == 0, result.output
    assert seen == [chosen]


def test_home_folder_falls_back_to_omnibase_path(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F3: from the home folder, the previous chain applies."""
    monkeypatch.setenv("OMNIBASE_PATH", str(tmp_path / "from-env"))
    result, seen = _run(monkeypatch, home, home)
    assert result.exit_code == 0, result.output
    assert seen == [tmp_path / "from-env"]


def test_home_folder_with_nothing_else_still_asks_for_a_workspace(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F3: the home folder is never silently made the workspace."""
    result, seen = _run(monkeypatch, home, home)
    assert result.exit_code != 0
    assert "--workspace is missing" in result.output
    assert seen == []


def test_filesystem_root_is_never_the_workspace(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F3."""
    result, seen = _run(monkeypatch, home, Path("/"))
    assert seen == []
    assert "--workspace is missing" in result.output
