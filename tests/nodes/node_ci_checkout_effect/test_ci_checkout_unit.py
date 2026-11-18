# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ``ci-checkout serve`` unit declaration: render, install, roll back, refuse (OMN-24287).

Exemplar followed: tests/nodes/node_repo_state_query_effect/test_repo_state_unit.py.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from importlib import resources
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from omnimarket.nodes.node_ci_checkout_effect.handlers.adapter_ci_checkout_serve import (
    serve_group,
)
from omnimarket.nodes.node_ci_checkout_effect.handlers.adapter_ci_checkout_unit import (
    CiCheckoutUnitInstaller,
    render_unit,
)
from omnimarket.nodes.node_ci_checkout_effect.models.model_ci_checkout_deployment import (
    ModelCiCheckoutDeployment,
)
from tests.nodes.node_ci_checkout_effect.planted import HOST, deployment_document

pytestmark = pytest.mark.unit
SERVICE = "test-ci-checkout-serve.service"


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


def _deployment(tmp_path: Path) -> ModelCiCheckoutDeployment:
    return ModelCiCheckoutDeployment.model_validate(
        deployment_document(tmp_path / "mirrors")
    )


def _layout(tmp_path: Path) -> tuple[Path, Path, Path]:
    omni = tmp_path / "omni"
    python = omni / "test/.venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    return omni, home, tmp_path / "overlay-root"


def test_golden_render_follows_the_overlay(tmp_path: Path) -> None:
    omni, home, overlays = _layout(tmp_path)
    text = render_unit(
        _deployment(tmp_path), HOST, omni_root=omni, home=home, overlay_root=overlays
    )
    assert (
        f'ExecStart="{omni}/test/.venv/bin/python" -m omnimarket.nodes.node_ci_checkout_effect serve'
        in text
    )
    assert f'--profile "inmemory" --host-name "{HOST}"' in text
    assert f'Environment="ONEX_SKILL_OVERLAY_ROOTS={overlays}"' in text
    assert (
        f"StandardOutput=append:{omni}/.onex_state/test-ci-checkout-serve/serve.log"
        in text
    )
    assert "Restart=on-failure" in text
    assert "$OMNI_HOME" not in text
    assert "$HOME" not in text


def test_golden_install_unchanged_and_rollback(tmp_path: Path) -> None:
    omni, home, overlays = _layout(tmp_path)
    runner = Calls()
    installer = CiCheckoutUnitInstaller(
        overlay=_deployment(tmp_path), home=home, run=runner
    )
    first = installer.install(HOST, omni_root=omni, overlay_root=overlays)
    unit_file = home / ".config/systemd/user" / SERVICE
    assert first.exit_code == 0, first.stderr
    assert unit_file.is_file()
    assert ["systemctl", "--user", "enable", "--now", SERVICE] in runner.calls
    assert (omni / ".onex_state/test-ci-checkout-serve").is_dir()
    assert (
        "unchanged"
        in installer.install(HOST, omni_root=omni, overlay_root=overlays).stdout
    )
    undone = installer.uninstall(HOST)
    assert undone.exit_code == 0
    assert not unit_file.exists()
    assert ["systemctl", "--user", "disable", "--now", SERVICE] in runner.calls


def test_golden_the_unit_command_renders_from_the_cli(tmp_path: Path) -> None:
    omni, _home, overlays = _layout(tmp_path)
    overlay_file = tmp_path / "overlay.yaml"
    overlay_file.write_text(yaml.safe_dump(deployment_document(tmp_path / "mirrors")))
    result = CliRunner().invoke(
        serve_group,
        [
            "unit",
            "render",
            "--overlay",
            str(overlay_file),
            "--host",
            HOST,
            "--omni-root",
            str(omni),
            "--overlay-root",
            str(overlays),
        ],
    )
    assert result.exit_code == 0, result.output
    assert f'--host-name "{HOST}"' in result.output


def test_error_an_undeclared_host_is_refused_and_nothing_is_written(
    tmp_path: Path,
) -> None:
    """Failing control: the same call for the declared host succeeds (the golden install above)."""
    omni, home, overlays = _layout(tmp_path)
    runner = Calls()
    installer = CiCheckoutUnitInstaller(
        overlay=_deployment(tmp_path), home=home, run=runner
    )
    result = installer.install("host-z", omni_root=omni, overlay_root=overlays)
    assert result.exit_code == 1
    assert "host-z" in result.stderr
    assert HOST in result.stderr
    assert runner.calls == []
    assert not (home / ".config").exists()
    assert installer.uninstall("host-z").exit_code == 1
    assert installer.status("host-z").exit_code == 1


def test_error_a_unit_that_runs_from_a_worktree_is_refused(tmp_path: Path) -> None:
    _omni, home, overlays = _layout(tmp_path)
    worktree = tmp_path / "omni_worktrees/lane/omnimarket"
    installer = CiCheckoutUnitInstaller(overlay=_deployment(tmp_path), home=home)
    result = installer.install(HOST, omni_root=worktree, overlay_root=overlays)
    assert result.exit_code == 1
    assert "omni_worktrees" in result.stderr
    assert not (home / ".config").exists()


def test_error_a_serve_module_that_does_not_import_leaves_no_unit(
    tmp_path: Path,
) -> None:
    omni, home, overlays = _layout(tmp_path)
    installer = CiCheckoutUnitInstaller(
        overlay=_deployment(tmp_path), home=home, run=Calls(fail_on="-c")
    )
    result = installer.install(HOST, omni_root=omni, overlay_root=overlays)
    assert result.exit_code == 1
    assert "cannot import" in result.stderr
    assert not (home / ".config").exists()


def test_error_a_missing_interpreter_leaves_no_unit(tmp_path: Path) -> None:
    omni, home, overlays = _layout(tmp_path)
    (omni / "test/.venv/bin/python").unlink()
    installer = CiCheckoutUnitInstaller(
        overlay=_deployment(tmp_path), home=home, run=Calls()
    )
    result = installer.install(HOST, omni_root=omni, overlay_root=overlays)
    assert result.exit_code == 1
    assert "no interpreter" in result.stderr
    assert not (home / ".config").exists()


def test_the_example_overlay_renders_for_its_declared_host(tmp_path: Path) -> None:
    """The seam to the deployed overlay: the node's own example is a document the installer renders."""
    example = yaml.safe_load(
        resources.files("omnimarket.nodes.node_ci_checkout_effect")
        .joinpath("overlay.example.yaml")
        .read_text()
    )
    deployment = ModelCiCheckoutDeployment.model_validate(example)
    omni, home, overlays = _layout(tmp_path)
    host = deployment.hosts[0].name
    text = render_unit(
        deployment, host, omni_root=omni, home=home, overlay_root=overlays
    )
    assert f'--host-name "{host}"' in text
