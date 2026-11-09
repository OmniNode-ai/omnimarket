# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The runner's schedule renders from the contract and the overlay, and uninstall is the rollback."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_commitment_check_runner_effect.handlers.schedule import (
    CommitmentCheckScheduler,
    load_schedule,
)
from omnimarket.nodes.node_commitment_check_runner_effect.models import (
    ModelCommitmentCheckRules,
)

pytestmark = pytest.mark.unit

_EXAMPLE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_commitment_check_runner_effect/overlay.example.yaml"
)
_HOST = "example-lab-host"


def _scheduler(
    tmp_path: Path, calls: list[list[str]], *, exit_code: int = 0
) -> CommitmentCheckScheduler:
    def run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(command))
        return subprocess.CompletedProcess(list(command), exit_code, "", "")

    rules = ModelCommitmentCheckRules.model_validate(
        yaml.safe_load(_EXAMPLE.read_text(encoding="utf-8"))
    )
    return CommitmentCheckScheduler(
        schedule=load_schedule(), overlay=rules, home=tmp_path / "home", run=run
    )


def test_install_writes_a_timer_that_ticks_on_the_contract_interval(
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []
    result = _scheduler(tmp_path, calls).install(
        _HOST, omni_root=tmp_path / "root", overlay_root=tmp_path / "ovl"
    )
    assert result.exit_code == 0, result.stderr
    unit_dir = tmp_path / "home/.config/systemd/user"
    service = (unit_dir / "onex-commitment-check.service").read_text()
    timer = (unit_dir / "onex-commitment-check.timer").read_text()
    assert "-m omnimarket.nodes.node_commitment_check_runner_effect\n" in service
    assert 'Environment="ONEX_HOST_NAME=example-lab-host"' in service
    assert "TimeoutStartSec=600" in service
    assert "OnUnitActiveSec=600s" in timer
    assert "OnBootSec=120s" in timer
    assert [
        "systemctl",
        "--user",
        "enable",
        "--now",
        "onex-commitment-check.timer",
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
        "onex-commitment-check.timer",
    ] in calls


def test_failing_control_a_host_the_overlay_does_not_declare_is_refused(
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []
    result = _scheduler(tmp_path, calls).install(
        "other", omni_root=tmp_path, overlay_root=tmp_path
    )
    assert result.exit_code == 1
    assert "schedule_hosts" in result.stderr
    assert not (tmp_path / "home").exists()
    assert calls == []


def test_failing_control_an_interpreter_that_cannot_import_the_node_installs_nothing(
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []
    result = _scheduler(tmp_path, calls, exit_code=1).install(
        _HOST, omni_root=tmp_path / "root", overlay_root=tmp_path / "ovl"
    )
    assert result.exit_code == 1
    assert "cannot import" in result.stderr
    assert not (tmp_path / "home/.config/systemd/user").exists()
