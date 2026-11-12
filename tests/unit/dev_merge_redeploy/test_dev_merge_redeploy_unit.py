# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ``dev-merge-redeploy serve`` unit declaration: render, install, roll back, refuse (OMN-22478, R2)."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner
from pydantic import ValidationError

from omnimarket.models.dev_merge_redeploy_overlay import ModelDevMergeRedeployPolicy
from omnimarket.nodes.node_dev_merge_redeploy_trigger_effect.__main__ import group
from omnimarket.nodes.node_dev_merge_redeploy_trigger_effect.handlers.adapter_dev_merge_redeploy_unit import (
    DevMergeUnitInstaller,
    render_unit,
)
from tests.unit.dev_merge_redeploy.support import policy

pytestmark = pytest.mark.unit

PROBE = ["probe-lane"]
SERVICE = "example-dev-merge-redeploy-serve.service"
UNIT: dict[str, Any] = {
    "unit_name": "example-dev-merge-redeploy-serve",
    "description": "Example dev-merge redeploy serve process",
    "python": "$OMNI_HOME/example/.venv/bin/python",
    "bus": "kafka",
    "bus_lane": "example-lane",
    "restart_seconds": 10,
    "stop_timeout_seconds": 30,
    "log_file": "$HOME/.local/state/example/serve.log",
    "environment": {"OMNIBASE_PATH": "$OMNI_HOME/example-workspace"},
    "hosts": [{"name": "host-a", "scheduler": "systemd-user"}],
}


def _document(unit: dict[str, Any] | None = UNIT) -> dict[str, Any]:
    document = policy().model_dump(mode="json") | {"lane_probe_argv": PROBE}
    return document | ({"unit": unit} if unit is not None else {})


def _policy(unit: dict[str, Any] | None = UNIT) -> ModelDevMergeRedeployPolicy:
    return ModelDevMergeRedeployPolicy.model_validate(_document(unit))


class Calls:
    """A recording runner standing in for systemctl, the import probe and the lane probe."""

    def __init__(self, fail_on: str | None = None, stderr: str = "boom") -> None:
        self.calls: list[list[str]] = []
        self.fail_on = fail_on
        self.stderr = stderr

    def __call__(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(command))
        failed = self.fail_on is not None and self.fail_on in command
        return subprocess.CompletedProcess(
            list(command), 1 if failed else 0, "", self.stderr if failed else ""
        )


def _layout(tmp_path: Path, root: str = "omni") -> tuple[Path, Path, Path]:
    omni = tmp_path / root
    python = omni / "example/.venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    return omni, home, tmp_path / "overlay-root"


def test_golden_render_follows_the_overlay(tmp_path: Path) -> None:
    omni, home, overlays = _layout(tmp_path)
    text = render_unit(
        _policy(), "host-a", omni_root=omni, home=home, overlay_root=overlays
    )
    assert (
        f'ExecStart="{omni}/example/.venv/bin/python" -m '
        "omnimarket.nodes.node_dev_merge_redeploy_trigger_effect "
        'serve --bus "kafka" --bus-lane "example-lane" --host-name "host-a"'
    ) in text
    assert f'Environment="ONEX_SKILL_OVERLAY_ROOTS={overlays}"' in text
    assert f'Environment="OMNIBASE_PATH={omni}/example-workspace"' in text
    assert "Restart=on-failure" in text
    assert "$OMNI_HOME" not in text


def test_golden_install_probes_the_lane_before_writing_then_rolls_back(
    tmp_path: Path,
) -> None:
    omni, home, overlays = _layout(tmp_path)
    runner = Calls()
    installer = DevMergeUnitInstaller(policy=_policy(), home=home, run=runner)
    first = installer.install("host-a", omni_root=omni, overlay_root=overlays)
    unit_file = home / ".config/systemd/user" / SERVICE
    assert first.exit_code == 0
    assert unit_file.is_file()
    reload_call = ["systemctl", "--user", "daemon-reload"]
    enable_call = ["systemctl", "--user", "enable", "--now", SERVICE]
    assert PROBE in runner.calls
    assert runner.calls.index(PROBE) < runner.calls.index(reload_call)
    assert runner.calls.index(reload_call) < runner.calls.index(enable_call)
    again = installer.install("host-a", omni_root=omni, overlay_root=overlays)
    assert "unchanged" in again.stdout
    back = installer.uninstall("host-a")
    assert back.exit_code == 0
    assert not unit_file.exists()
    assert ["systemctl", "--user", "disable", "--now", SERVICE] in runner.calls


def test_control_a_lane_probe_that_names_a_missing_container_is_refused_not_installed(
    tmp_path: Path,
) -> None:
    omni, home, overlays = _layout(tmp_path)
    runner = Calls(fail_on="probe-lane", stderr="No such container: missing")
    installer = DevMergeUnitInstaller(policy=_policy(), home=home, run=runner)
    result = installer.install("host-a", omni_root=omni, overlay_root=overlays)
    assert result.exit_code == 1
    assert "deadline_missed" in result.stderr
    assert "No such container" in result.stderr
    assert not (home / ".config/systemd/user" / SERVICE).exists()
    assert not [c for c in runner.calls if c[0] == "systemctl"]


def test_error_no_unit_block_refuses_every_verb(tmp_path: Path) -> None:
    omni, home, overlays = _layout(tmp_path)
    installer = DevMergeUnitInstaller(policy=_policy(None), home=home, run=Calls())
    results = [
        installer.render("host-a", omni_root=omni, overlay_root=overlays),
        installer.install("host-a", omni_root=omni, overlay_root=overlays),
        installer.uninstall("host-a"),
        installer.status("host-a"),
    ]
    assert [r.exit_code for r in results] == [1, 1, 1, 1]
    assert all("no unit block" in r.stderr for r in results)


def test_error_undeclared_host_refuses(tmp_path: Path) -> None:
    omni, home, overlays = _layout(tmp_path)
    runner = Calls()
    installer = DevMergeUnitInstaller(policy=_policy(), home=home, run=runner)
    result = installer.install("host-z", omni_root=omni, overlay_root=overlays)
    assert result.exit_code == 1
    assert "host-z" in result.stderr
    assert installer.uninstall("host-z").exit_code == 1
    assert runner.calls == []


def test_error_missing_interpreter_refuses_and_writes_nothing(tmp_path: Path) -> None:
    omni, home, overlays = _layout(tmp_path)
    (omni / "example/.venv/bin/python").unlink()
    runner = Calls()
    installer = DevMergeUnitInstaller(policy=_policy(), home=home, run=runner)
    result = installer.install("host-a", omni_root=omni, overlay_root=overlays)
    assert result.exit_code == 1
    assert "no interpreter" in result.stderr
    assert not (home / ".config/systemd/user" / SERVICE).exists()


def test_error_a_unit_run_from_a_lane_worktree_is_refused(tmp_path: Path) -> None:
    omni, home, overlays = _layout(tmp_path, "omni_worktrees/OMN-1/omnimarket")
    installer = DevMergeUnitInstaller(policy=_policy(), home=home, run=Calls())
    result = installer.install("host-a", omni_root=omni, overlay_root=overlays)
    assert result.exit_code == 1
    assert not (home / ".config/systemd/user" / SERVICE).exists()


def test_error_a_unit_the_serve_cannot_start_from_is_refused_by_the_model() -> None:
    with pytest.raises(ValidationError, match="OMNIBASE_PATH"):
        _policy(UNIT | {"environment": {"OTHER": "x"}})
    with pytest.raises(ValidationError, match="bus_lane"):
        _policy(UNIT | {"bus_lane": None})


def test_golden_cli_render_reads_the_overlay_root(tmp_path: Path) -> None:
    omni, _home, overlays = _layout(tmp_path)
    node_dir = overlays / "node_dev_merge_redeploy_trigger_effect"
    node_dir.mkdir(parents=True)
    (node_dir / "overlay.yaml").write_text(
        yaml.safe_dump(_document()), encoding="utf-8"
    )
    args = [
        "unit",
        "render",
        "--host",
        "host-a",
        "--omni-root",
        str(omni),
        "--overlay-root",
        str(overlays),
    ]
    env = {"ONEX_SKILL_OVERLAY_ROOTS": str(overlays)}
    done = CliRunner().invoke(group, args, env=env)
    assert done.exit_code == 0, done.output
    assert '--host-name "host-a"' in done.output
    missing = CliRunner().invoke(
        group, args, env={"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "none")}
    )
    assert missing.exit_code == 78
