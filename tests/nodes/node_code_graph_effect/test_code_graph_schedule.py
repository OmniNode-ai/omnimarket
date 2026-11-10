# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The nightly schedule: rendered from the contract and the overlay, installed and removed (OMN-22651)."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from omnimarket.nodes.node_code_graph_effect.handlers.schedule import (
    CodeGraphScheduler,
    load_schedule,
)
from tests.nodes.node_code_graph_effect.code_graph_support import overlay

pytestmark = pytest.mark.unit


class _Runner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(command))
        return subprocess.CompletedProcess(list(command), 0, "", "")


def _scheduler(home: Path, runner: _Runner) -> CodeGraphScheduler:
    return CodeGraphScheduler(
        schedule=load_schedule(), overlay=overlay(), home=home, run=runner
    )


def test_dry_run_renders_the_nightly_timer_and_service(tmp_path: Path) -> None:
    result = _scheduler(tmp_path / "home", _Runner()).install(
        "example-host",
        omni_root=tmp_path / "root",
        overlay_root=tmp_path / "overlays",
        dry_run=True,
    )
    assert result.exit_code == 0, result.stderr
    assert "OnCalendar=*-*-* 06:40:00 UTC" in result.stdout
    assert "-m omnimarket.nodes.node_code_graph_effect nightly" in result.stdout
    assert f'Environment="EXAMPLE_REPOS_ROOT={tmp_path / "root"}"' in result.stdout
    assert "Persistent=true" in result.stdout


def test_install_enables_the_timer_and_uninstall_removes_it(tmp_path: Path) -> None:
    root = tmp_path / "root"
    python = root / "example/.venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    runner = _Runner()
    scheduler = _scheduler(tmp_path / "home", runner)
    installed = scheduler.install(
        "example-host", omni_root=root, overlay_root=tmp_path / "o"
    )
    assert installed.exit_code == 0, installed.stderr
    assert [
        "systemctl",
        "--user",
        "enable",
        "--now",
        "onex-code-graph-nightly.timer",
    ] in runner.calls
    timer = tmp_path / "home/.config/systemd/user/onex-code-graph-nightly.timer"
    assert timer.is_file()
    removed = scheduler.uninstall("example-host")
    assert removed.exit_code == 0
    assert not timer.exists()


def test_an_undeclared_host_is_refused(tmp_path: Path) -> None:
    result = _scheduler(tmp_path, _Runner()).install(
        "other", omni_root=tmp_path, overlay_root=tmp_path
    )
    assert result.exit_code == 1
    assert "not in the node_code_graph_effect overlay" in result.stderr


def test_the_shadow_unit_renders_its_own_timer_and_runs_the_shadow_verb(
    tmp_path: Path,
) -> None:
    scheduler = CodeGraphScheduler(
        schedule=load_schedule(block="shadow_schedule"),
        overlay=overlay(),
        home=tmp_path / "home",
        run=_Runner(),
    )
    result = scheduler.install(
        "example-host",
        omni_root=tmp_path / "root",
        overlay_root=tmp_path / "overlays",
        dry_run=True,
    )
    assert result.exit_code == 0, result.stderr
    assert "OnCalendar=*-*-* *:20:00 UTC" in result.stdout
    assert "-m omnimarket.nodes.node_code_graph_effect shadow\n" in result.stdout
    assert "code-graph-shadow.out.log" in result.stdout
    assert (
        f'Environment="EXAMPLE_PR_STATE_PATH={tmp_path / "root"}/.onex_state'
        in result.stdout
    )
    assert scheduler.timer_name == "onex-code-graph-shadow.timer"


def test_the_two_units_do_not_share_a_name_and_the_nightly_text_is_unchanged(
    tmp_path: Path,
) -> None:
    nightly = load_schedule()
    shadow = load_schedule(block="shadow_schedule")
    assert nightly.unit_name != shadow.unit_name
    assert nightly.verb == "nightly"
    assert nightly.timer_description == "ONEX nightly code graph tick"
    assert nightly.description == "ONEX nightly code graph (piece 1 reading)"
