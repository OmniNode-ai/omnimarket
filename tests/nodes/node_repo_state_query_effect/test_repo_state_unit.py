# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ``repo-state serve`` unit declaration: render, install, roll back, refuse (OMN-23033, AC2)."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner
from pydantic import ValidationError

from omnimarket.nodes.node_repo_state_query_effect.handlers.adapter_repo_state_query_serve import (
    _bus_args,
    load_overlay,
    repo_state_group,
)
from omnimarket.nodes.node_repo_state_query_effect.handlers.adapter_repo_state_unit import (
    RepoStateUnitInstaller,
    render_unit,
)
from omnimarket.nodes.node_repo_state_query_effect.models.model_repo_state_overlay import (
    ModelRepoStateOverlay,
)

pytestmark = pytest.mark.unit
_EXAMPLE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_repo_state_query_effect/overlay.example.yaml"
)


class Calls:
    """A recording runner standing in for systemctl and the import probe."""

    def __init__(self, fail_on: str | None = None) -> None:
        self.calls: list[list[str]] = []
        self.fail_on = fail_on

    def __call__(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(command))
        failed = self.fail_on is not None and self.fail_on in command
        return subprocess.CompletedProcess(
            list(command), 1 if failed else 0, "", "boom" if failed else ""
        )


def _layout(tmp_path: Path) -> tuple[Path, Path, Path]:
    omni = tmp_path / "omni"
    python = omni / "example/.venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    return omni, home, tmp_path / "overlay-root"


def test_golden_render_follows_the_overlay(tmp_path: Path) -> None:
    omni, home, overlays = _layout(tmp_path)
    text = render_unit(
        load_overlay(_EXAMPLE),
        "host-a",
        omni_root=omni,
        home=home,
        overlay_root=overlays,
    )
    assert (
        f'ExecStart="{omni}/example/.venv/bin/python" -m '
        'omnimarket.nodes.node_repo_state_query_effect serve --profile "redpanda" '
        '--host-name "host-a"'
    ) in text
    assert f'Environment="EXAMPLE_CLONES_ROOT={omni}"' in text
    assert f'Environment="EXAMPLE_WORKSPACE_ROOT={omni}/example-workspace"' in text
    assert f'Environment="ONEX_SKILL_OVERLAY_ROOTS={overlays}"' in text
    assert "PYTHONPATH" not in text
    assert "Restart=on-failure" in text
    assert "$OMNI_HOME" not in text


def test_golden_install_unchanged_and_rollback(tmp_path: Path) -> None:
    omni, home, overlays = _layout(tmp_path)
    runner = Calls()
    installer = RepoStateUnitInstaller(
        overlay=load_overlay(_EXAMPLE), home=home, run=runner
    )
    first = installer.install("host-a", omni_root=omni, overlay_root=overlays)
    unit_file = home / ".config/systemd/user/example-repo-state-serve.service"
    assert first.exit_code == 0, first.stderr
    assert unit_file.is_file()
    assert [
        "systemctl",
        "--user",
        "enable",
        "--now",
        "example-repo-state-serve.service",
    ] in runner.calls
    assert (omni / ".onex_state/example-repo-state").is_dir()
    assert (
        "unchanged"
        in installer.install("host-a", omni_root=omni, overlay_root=overlays).stdout
    )
    undone = installer.uninstall("host-a")
    assert undone.exit_code == 0
    assert not unit_file.exists()
    assert [
        "systemctl",
        "--user",
        "disable",
        "--now",
        "example-repo-state-serve.service",
    ] in runner.calls


def test_golden_the_named_lane_resolves_its_workspace_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    overlay = load_overlay(_EXAMPLE)
    monkeypatch.setenv("EXAMPLE_WORKSPACE_ROOT", "/srv/workspace")
    assert _bus_args(overlay, "redpanda") == (
        "kafka",
        "example-lane",
        None,
        Path("/srv/workspace"),
    )
    assert _bus_args(overlay, "inmemory") == ("inmemory", None, None, None)


def test_golden_the_unit_command_renders_from_the_cli(tmp_path: Path) -> None:
    omni, _home, overlays = _layout(tmp_path)
    result = CliRunner().invoke(
        repo_state_group,
        [
            "unit",
            "render",
            "--overlay",
            str(_EXAMPLE),
            "--host",
            "host-a",
            "--omni-root",
            str(omni),
            "--overlay-root",
            str(overlays),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "serve --profile" in result.output


def test_error_an_undeclared_host_is_refused_and_nothing_is_written(
    tmp_path: Path,
) -> None:
    omni, home, overlays = _layout(tmp_path)
    runner = Calls()
    installer = RepoStateUnitInstaller(
        overlay=load_overlay(_EXAMPLE), home=home, run=runner
    )
    result = installer.install("host-z", omni_root=omni, overlay_root=overlays)
    assert result.exit_code == 1
    assert "host-z" in result.stderr
    assert runner.calls == []
    assert not (home / ".config").exists()
    assert installer.uninstall("host-z").exit_code == 1
    assert installer.status("host-z").exit_code == 1


def test_error_a_unit_that_runs_from_a_worktree_is_refused(tmp_path: Path) -> None:
    _omni, home, overlays = _layout(tmp_path)
    worktree = tmp_path / "omni_worktrees/lane/omnimarket"
    installer = RepoStateUnitInstaller(overlay=load_overlay(_EXAMPLE), home=home)
    result = installer.install("host-a", omni_root=worktree, overlay_root=overlays)
    assert result.exit_code == 1
    assert "omni_worktrees" in result.stderr
    assert not (home / ".config").exists()


def test_error_a_serve_module_that_does_not_import_leaves_no_unit(
    tmp_path: Path,
) -> None:
    omni, home, overlays = _layout(tmp_path)
    runner = Calls(fail_on="-c")
    installer = RepoStateUnitInstaller(
        overlay=load_overlay(_EXAMPLE), home=home, run=runner
    )
    result = installer.install("host-a", omni_root=omni, overlay_root=overlays)
    assert result.exit_code == 1
    assert "cannot import" in result.stderr
    assert not (home / ".config").exists()


def test_error_a_missing_interpreter_leaves_no_unit(tmp_path: Path) -> None:
    omni, home, overlays = _layout(tmp_path)
    (omni / "example/.venv/bin/python").unlink()
    installer = RepoStateUnitInstaller(
        overlay=load_overlay(_EXAMPLE), home=home, run=Calls()
    )
    result = installer.install("host-a", omni_root=omni, overlay_root=overlays)
    assert result.exit_code == 1
    assert "no interpreter" in result.stderr
    assert not (home / ".config").exists()


def _document() -> dict[str, object]:
    return yaml.safe_load(_EXAMPLE.read_text(encoding="utf-8"))


def test_injected_break_a_unit_with_no_value_for_a_named_variable_is_refused() -> None:
    """The control: the overlay names a variable the unit gives no value for."""
    document = _document()
    unit = document["unit"]
    assert isinstance(unit, dict)
    del unit["environment"]["EXAMPLE_CLONES_ROOT"]
    with pytest.raises(ValidationError, match="EXAMPLE_CLONES_ROOT"):
        ModelRepoStateOverlay.model_validate(document)
    assert ModelRepoStateOverlay.model_validate(_document()) is not None


def test_injected_break_a_unit_serving_a_profile_the_overlay_lacks_is_refused() -> None:
    document = _document()
    unit = document["unit"]
    assert isinstance(unit, dict)
    unit["bus_profile"] = "dev"
    with pytest.raises(ValidationError, match="not a profile"):
        ModelRepoStateOverlay.model_validate(document)
