# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The hourly schedule renders from the contract and the overlay, and uninstall is the rollback."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_seam_deadline_probe_effect.handlers.schedule import (
    SeamDeadlineScheduler,
    load_schedule,
)
from omnimarket.nodes.node_seam_deadline_probe_effect.overlay import (
    ModelSeamDeadlineOverlay,
)

pytestmark = pytest.mark.unit

_EXAMPLE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_seam_deadline_probe_effect/overlay.example.yaml"
)


def _scheduler(tmp_path: Path, calls: list[list[str]]) -> SeamDeadlineScheduler:
    def run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(command))
        return subprocess.CompletedProcess(list(command), 0, "", "")

    overlay = ModelSeamDeadlineOverlay.model_validate(
        yaml.safe_load(_EXAMPLE.read_text(encoding="utf-8"))
    )
    return SeamDeadlineScheduler(
        schedule=load_schedule(), overlay=overlay, home=tmp_path / "home", run=run
    )


def test_install_writes_an_hourly_timer_that_runs_the_read_verb(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    scheduler = _scheduler(tmp_path, calls)
    result = scheduler.install(
        "example-lab-host", omni_root=tmp_path / "root", overlay_root=tmp_path / "ovl"
    )
    assert result.exit_code == 0, result.stderr
    unit_dir = tmp_path / "home/.config/systemd/user"
    service = (unit_dir / "onex-seam-deadline-hourly.service").read_text()
    timer = (unit_dir / "onex-seam-deadline-hourly.timer").read_text()
    assert "-m omnimarket.nodes.node_seam_deadline_probe_effect read" in service
    assert 'Environment="EXAMPLE_HOST_NAME=example-lab-host"' in service
    assert "OnCalendar=*-*-* *:35:00" in timer
    assert [
        "systemctl",
        "--user",
        "enable",
        "--now",
        "onex-seam-deadline-hourly.timer",
    ] in calls


def test_uninstall_is_the_rollback(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    scheduler = _scheduler(tmp_path, calls)
    scheduler.install(
        "example-lab-host", omni_root=tmp_path / "root", overlay_root=tmp_path / "ovl"
    )
    result = scheduler.uninstall("example-lab-host")
    assert result.exit_code == 0
    assert not list((tmp_path / "home/.config/systemd/user").iterdir())
    assert [
        "systemctl",
        "--user",
        "disable",
        "--now",
        "onex-seam-deadline-hourly.timer",
    ] in calls


def test_a_host_the_overlay_does_not_declare_is_refused(tmp_path: Path) -> None:
    result = _scheduler(tmp_path, []).install(
        "other", omni_root=tmp_path, overlay_root=tmp_path
    )
    assert result.exit_code == 1
    assert "schedule_hosts" in result.stderr
