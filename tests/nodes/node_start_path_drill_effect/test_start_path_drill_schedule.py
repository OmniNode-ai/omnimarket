# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The daily schedule renders from the contract and the overlay, and uninstall is the rollback."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_start_path_drill_effect.handlers.schedule import (
    StartPathDrillScheduler,
    load_schedule,
)
from omnimarket.nodes.node_start_path_drill_effect.overlay import (
    ModelStartPathDrillOverlay,
)

pytestmark = pytest.mark.unit

_EXAMPLE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_start_path_drill_effect/overlay.example.yaml"
)
_HOST = "example-lab-host"


def _scheduler(tmp_path: Path, calls: list[list[str]]) -> StartPathDrillScheduler:
    def run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(command))
        return subprocess.CompletedProcess(list(command), 0, "", "")

    overlay = ModelStartPathDrillOverlay.model_validate(
        yaml.safe_load(_EXAMPLE.read_text(encoding="utf-8"))
    )
    return StartPathDrillScheduler(
        schedule=load_schedule(), overlay=overlay, home=tmp_path / "home", run=run
    )


def test_install_writes_a_daily_timer_that_runs_the_read_verb(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    result = _scheduler(tmp_path, calls).install(
        _HOST, omni_root=tmp_path / "root", overlay_root=tmp_path / "ovl"
    )
    assert result.exit_code == 0, result.stderr
    unit_dir = tmp_path / "home/.config/systemd/user"
    service = (unit_dir / "onex-start-path-drill-daily.service").read_text()
    timer = (unit_dir / "onex-start-path-drill-daily.timer").read_text()
    assert "-m omnimarket.nodes.node_start_path_drill_effect read" in service
    assert f'Environment="EXAMPLE_HOST_NAME={_HOST}"' in service
    assert "OnCalendar=*-*-* 06:40:00" in timer
    assert [
        "systemctl",
        "--user",
        "enable",
        "--now",
        "onex-start-path-drill-daily.timer",
    ] in calls


def test_uninstall_is_the_rollback(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    scheduler = _scheduler(tmp_path, calls)
    scheduler.install(_HOST, omni_root=tmp_path / "root", overlay_root=tmp_path / "ovl")
    result = scheduler.uninstall(_HOST)
    assert result.exit_code == 0
    assert not list((tmp_path / "home/.config/systemd/user").iterdir())
    assert [
        "systemctl",
        "--user",
        "disable",
        "--now",
        "onex-start-path-drill-daily.timer",
    ] in calls


def test_a_host_the_overlay_does_not_declare_is_refused(tmp_path: Path) -> None:
    result = _scheduler(tmp_path, []).install(
        "other", omni_root=tmp_path, overlay_root=tmp_path
    )
    assert result.exit_code == 1
    assert "schedule_hosts" in result.stderr


def test_a_dry_run_writes_no_unit_and_runs_no_command(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    result = _scheduler(tmp_path, calls).install(
        _HOST,
        omni_root=tmp_path / "root",
        overlay_root=tmp_path / "ovl",
        dry_run=True,
    )
    assert result.exit_code == 0
    assert "onex-start-path-drill-daily" in result.stdout
    assert calls == []
    assert not (tmp_path / "home").exists()


def test_a_root_a_unit_line_cannot_carry_is_refused(tmp_path: Path) -> None:
    result = _scheduler(tmp_path, []).install(
        _HOST, omni_root=tmp_path / "a root", overlay_root=tmp_path / "ovl"
    )
    assert result.exit_code == 1
    assert "registry root" in result.stderr
