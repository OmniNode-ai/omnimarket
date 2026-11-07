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


# --- the scheduled drill: the error event of piece 1 -----------------------------------------------------------------


def test_dry_run_renders_the_drill_pair_beside_the_nightly_pair(tmp_path: Path) -> None:
    result = _scheduler(tmp_path / "home", _Runner()).install(
        "example-host",
        omni_root=tmp_path / "root",
        overlay_root=tmp_path / "overlays",
        dry_run=True,
    )
    assert result.exit_code == 0, result.stderr
    assert "OnCalendar=*-*-* 00,12:25:00 UTC" in result.stdout
    assert (
        "-m omnimarket.nodes.node_code_graph_effect nightly --fault unreadable-repo"
        in result.stdout
    )
    assert "SyslogIdentifier=onex-code-graph-drill" in result.stdout
    # the nightly service still runs with no fault
    assert "node_code_graph_effect nightly\n" in result.stdout


def test_install_enables_both_timers_and_uninstall_removes_both(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    python = root / "example/.venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    runner = _Runner()
    scheduler = _scheduler(tmp_path / "home", runner)
    assert (
        scheduler.install(
            "example-host", omni_root=root, overlay_root=tmp_path / "o"
        ).exit_code
        == 0
    )
    for timer in ("onex-code-graph-nightly.timer", "onex-code-graph-drill.timer"):
        assert ["systemctl", "--user", "enable", "--now", timer] in runner.calls
        assert (tmp_path / "home/.config/systemd/user" / timer).is_file()
    assert scheduler.uninstall("example-host").exit_code == 0
    for unit in (
        "onex-code-graph-nightly.timer",
        "onex-code-graph-drill.timer",
        "onex-code-graph-drill.service",
    ):
        assert not (tmp_path / "home/.config/systemd/user" / unit).exists()


def test_run_now_drill_starts_the_drill_service_once(tmp_path: Path) -> None:
    runner = _Runner()
    result = _scheduler(tmp_path, runner).run_now("example-host", drill=True)
    assert result.exit_code == 0
    assert [
        "systemctl",
        "--user",
        "start",
        "--no-block",
        "onex-code-graph-drill.service",
    ] in runner.calls


def test_error_named_break_drill_dropped_leaves_no_drill_unit(tmp_path: Path) -> None:
    schedule = load_schedule().model_copy(update={"drill": None})
    scheduler = CodeGraphScheduler(
        schedule=schedule, overlay=overlay(), home=tmp_path / "home", run=_Runner()
    )
    result = scheduler.install(
        "example-host",
        omni_root=tmp_path / "root",
        overlay_root=tmp_path / "o",
        dry_run=True,
    )
    assert "--fault" not in result.stdout
    assert scheduler.run_now("example-host", drill=True).exit_code == 1
