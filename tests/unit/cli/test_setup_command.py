# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21311: ``onex setup`` runs the onboarding walk from the CLI.

AC2  on a clean HOME the walk completes without prompts where every value can
     be derived, and prompts only for the one value that cannot be (the
     workspace root) when nothing names it.
AC3  a second run on a configured HOME changes nothing and exits 0.
AC4  a failed step names its fix and the command to retry, and the retry
     resumes and completes once the fix is applied.
AC5  (setup half) ``onex setup`` is registered, its help claims machine setup
     and the new-developer path, and no core command (init, bootstrap, install)
     claims machine setup. The core half lives in omnibase_core's
     tests/unit/cli/test_setup_help_text.py.

Every test runs against a temporary HOME and workspace; the real home is
never read or written (``_isolated_home`` asserts that).
"""

from __future__ import annotations

import hashlib
import re
import shlex
from collections.abc import Iterator
from pathlib import Path

import click
import pytest
from click.testing import CliRunner, Result
from omnibase_core.cli.cli_commands import cli

from omnimarket.nodes.node_onex_setup_effect.handlers import handler_onex_setup

_UNSET_ENV: dict[str, str | None] = {"OMNIBASE_PATH": None, "OMNI_HOME": None}


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A clean HOME; the real home directory is never used."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    real_home = Path.home()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("OMNIBASE_PATH", raising=False)
    monkeypatch.delenv("OMNI_HOME", raising=False)
    assert Path.home() == fake_home
    assert Path.home() != real_home
    # Every prerequisite present by default; tests remove what they need to.
    monkeypatch.setattr(handler_onex_setup, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(handler_onex_setup, "compose_available", lambda: True)
    return fake_home


def _run(args: list[str], *, prompt_input: str | None = None, **env: str) -> Result:
    merged: dict[str, str | None] = dict(_UNSET_ENV)
    merged.update(env)
    return CliRunner().invoke(cli, ["setup", *args], input=prompt_input, env=merged)


def _snapshot(*roots: Path) -> dict[str, str]:
    """Path -> content hash (or 'dir') of every entry under the roots."""
    state: dict[str, str] = {}
    for root in roots:
        for path in sorted(root.rglob("*")):
            key = str(path)
            if path.is_dir():
                state[key] = "dir"
            else:
                state[key] = hashlib.sha256(path.read_bytes()).hexdigest()
                state[key + "#mtime"] = str(path.stat().st_mtime_ns)
    return state


def _prompts(result: Result) -> list[str]:
    return [line for line in result.output.splitlines() if line.endswith(": ")]


@pytest.mark.unit
class TestSetupWalk:
    """AC2: non-interactive where it can, prompts only for underivable values."""

    def test_clean_home_with_workspace_named_runs_without_prompts(
        self, home: Path, tmp_path: Path
    ) -> None:
        workspace = tmp_path / "ws"
        result = _run(["--interactive"], prompt_input="", OMNIBASE_PATH=str(workspace))

        assert result.exit_code == 0, result.output
        assert "Workspace root" not in result.output, "prompted for a derivable value"
        runtime = workspace / "config/onex/runtime/runtime_config.yaml"
        assert runtime.read_text().splitlines()[0] == handler_onex_setup.SETUP_MARKER
        assert 'type: "inmemory"' in runtime.read_text()
        assert (home / ".onex/config.yaml").is_file()
        assert (home / ".onex/setup.json").is_file()
        assert f'export OMNIBASE_PATH="{workspace}"' in result.output

    def test_omni_home_bridge_is_a_derivation_source(
        self, home: Path, tmp_path: Path
    ) -> None:
        workspace = tmp_path / "bridge"
        result = _run(["--no-interactive"], OMNI_HOME=str(workspace))
        assert result.exit_code == 0, result.output
        assert (workspace / "config/onex/runtime/runtime_config.yaml").is_file()

    def test_scripted_empty_environment_prompts_once_for_the_workspace(
        self, home: Path, tmp_path: Path
    ) -> None:
        workspace = tmp_path / "typed"
        result = _run(["--interactive"], prompt_input=f"{workspace}\n")

        assert result.exit_code == 0, result.output
        prompts = [
            line for line in result.output.splitlines() if "Workspace root" in line
        ]
        assert len(prompts) == 1, result.output
        assert "Install path" not in result.output, "prompted for a defaulted value"
        assert (workspace / "config/onex/runtime/runtime_config.yaml").is_file()

    def test_empty_environment_without_a_terminal_stops_and_names_the_fix(
        self, home: Path
    ) -> None:
        result = _run(["--no-interactive"])

        assert result.exit_code == 1
        assert "Workspace root" not in result.output, "prompted without a terminal"
        assert "step 'workspace' failed" in result.output
        assert "--workspace" in result.output
        assert "export OMNIBASE_PATH" in result.output
        assert not (home / ".onex").exists(), "wrote state before the walk could run"

    def test_remembered_workspace_is_derived_on_the_next_run(
        self, home: Path, tmp_path: Path
    ) -> None:
        workspace = tmp_path / "remembered"
        first = _run(["--no-interactive", "--workspace", str(workspace)])
        assert first.exit_code == 0, first.output
        second = _run(["--interactive"], prompt_input="")
        assert second.exit_code == 0, second.output
        assert "Workspace root" not in second.output

    def test_docker_path_checks_its_prerequisites(
        self, home: Path, tmp_path: Path
    ) -> None:
        workspace = tmp_path / "ws"
        result = _run(
            ["--no-interactive", "--path", "docker"], OMNIBASE_PATH=str(workspace)
        )
        assert result.exit_code == 0, result.output
        runtime = workspace / "config/onex/runtime/runtime_config.yaml"
        assert 'type: "kafka"' in runtime.read_text()
        assert "# install-path: docker" in runtime.read_text()

    def test_cloud_path_is_refused_with_the_choices(
        self, home: Path, tmp_path: Path
    ) -> None:
        result = _run(
            ["--no-interactive", "--path", "cloud"], OMNIBASE_PATH=str(tmp_path / "ws")
        )
        assert result.exit_code == 1
        assert "--path local" in result.output


@pytest.mark.unit
class TestSetupIdempotent:
    """AC3: a second run changes nothing and exits 0."""

    @pytest.mark.parametrize("path_choice", ["local", "docker"])
    def test_second_run_leaves_identical_state(
        self, home: Path, tmp_path: Path, path_choice: str
    ) -> None:
        workspace = tmp_path / "ws"
        args = [
            "--no-interactive",
            "--workspace",
            str(workspace),
            "--path",
            path_choice,
        ]
        first = _run(args)
        assert first.exit_code == 0, first.output
        before = _snapshot(home, workspace)

        second = _run(args)
        assert second.exit_code == 0, second.output
        assert _snapshot(home, workspace) == before
        assert "unchanged" in second.output

    def test_second_run_with_no_arguments_keeps_the_chosen_path(
        self, home: Path, tmp_path: Path
    ) -> None:
        workspace = tmp_path / "ws"
        first = _run(
            ["--no-interactive", "--workspace", str(workspace), "--path", "docker"]
        )
        assert first.exit_code == 0, first.output
        before = _snapshot(home, workspace)

        second = _run(["--no-interactive"])
        assert second.exit_code == 0, second.output
        assert _snapshot(home, workspace) == before

    def test_existing_user_config_is_never_rewritten(
        self, home: Path, tmp_path: Path
    ) -> None:
        config = home / ".onex/config.yaml"
        config.parent.mkdir(parents=True)
        config.write_text("version: 1\nmode: local\n")
        result = _run(["--no-interactive", "--workspace", str(tmp_path / "ws")])
        assert result.exit_code == 0, result.output
        assert config.read_text() == "version: 1\nmode: local\n"


def _break_workspace(home: Path, workspace: Path, mp: pytest.MonkeyPatch) -> list[str]:
    workspace.parent.mkdir(parents=True, exist_ok=True)
    workspace.write_text("a file where the workspace directory should be\n")
    return ["--no-interactive", "--workspace", str(workspace)]


def _fix_workspace(home: Path, workspace: Path, mp: pytest.MonkeyPatch) -> None:
    workspace.unlink()


def _break_prerequisites(
    home: Path, workspace: Path, mp: pytest.MonkeyPatch
) -> list[str]:
    mp.setattr(
        handler_onex_setup,
        "which",
        lambda name: None if name == "docker" else f"/usr/bin/{name}",
    )
    return ["--no-interactive", "--workspace", str(workspace), "--path", "docker"]


def _fix_prerequisites(home: Path, workspace: Path, mp: pytest.MonkeyPatch) -> None:
    mp.setattr(handler_onex_setup, "which", lambda name: f"/usr/bin/{name}")


def _break_user_config(
    home: Path, workspace: Path, mp: pytest.MonkeyPatch
) -> list[str]:
    config = home / ".onex/config.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("mode: [unclosed\n")
    return ["--no-interactive", "--workspace", str(workspace)]


def _fix_user_config(home: Path, workspace: Path, mp: pytest.MonkeyPatch) -> None:
    (home / ".onex/config.yaml").unlink()


def _break_runtime_config(
    home: Path, workspace: Path, mp: pytest.MonkeyPatch
) -> list[str]:
    runtime = workspace / "config/onex/runtime/runtime_config.yaml"
    runtime.parent.mkdir(parents=True, exist_ok=True)
    runtime.write_text("name: hand-written\n")
    return ["--no-interactive", "--workspace", str(workspace)]


def _fix_runtime_config(home: Path, workspace: Path, mp: pytest.MonkeyPatch) -> None:
    runtime = workspace / "config/onex/runtime/runtime_config.yaml"
    runtime.rename(runtime.with_suffix(".yaml.bak"))


_FAILURES = {
    "workspace": (_break_workspace, _fix_workspace),
    "prerequisites": (_break_prerequisites, _fix_prerequisites),
    "user_config": (_break_user_config, _fix_user_config),
    "runtime_config": (_break_runtime_config, _fix_runtime_config),
}


@pytest.mark.unit
class TestSetupFailureAndResume:
    """AC4: each step's failure names one fix and a retry, and the retry completes."""

    def test_every_step_has_an_injected_failure(self) -> None:
        assert set(_FAILURES) == set(handler_onex_setup.SETUP_STEPS)

    @pytest.mark.parametrize("step", sorted(_FAILURES))
    def test_failed_step_names_fix_and_retry_then_resumes(
        self, home: Path, tmp_path: Path, step: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        workspace = tmp_path / "ws"
        brk, fix = _FAILURES[step]
        args = brk(home, workspace, monkeypatch)

        failed = _run(args)
        assert failed.exit_code == 1, failed.output
        assert f"step '{step}' failed" in failed.output
        fix_lines = [
            line for line in failed.output.splitlines() if line.startswith("Fix: ")
        ]
        assert len(fix_lines) == 1, failed.output
        retry = re.search(r"^Retry: (onex setup.*)$", failed.output, re.MULTILINE)
        assert retry, failed.output
        retry_args = shlex.split(retry.group(1))[2:]

        fix(home, workspace, monkeypatch)
        resumed = _run(retry_args)
        assert resumed.exit_code == 0, resumed.output
        assert (workspace / "config/onex/runtime/runtime_config.yaml").is_file()
        assert (home / ".onex/setup.json").is_file()


def _help(*args: str) -> str:
    result = CliRunner().invoke(cli, [*args, "--help"])
    assert result.exit_code == 0, result.output
    return " ".join(result.output.split())


_SETUP_JOB = "set up this machine"


@pytest.mark.unit
class TestSetupHelpText:
    """AC5, setup half: setup owns machine setup and no core command claims it."""

    def test_setup_is_registered_on_the_onex_cli(self) -> None:
        assert "setup" in cli.list_commands(click.Context(cli))

    def test_setup_claims_machine_setup_for_a_new_developer(self) -> None:
        text = _help("setup").lower()
        assert _SETUP_JOB in text
        assert "new developer" in text

    @pytest.mark.parametrize("name", ["init", "bootstrap", "install"])
    def test_no_core_command_claims_machine_setup(self, name: str) -> None:
        assert _SETUP_JOB not in _help(name).lower(), name
