# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Review findings 5347:11 and 5347:12 on the commitment check schedule (OMN-23013); each failed before the fix."""

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
    tmp_path: Path, outcomes: dict[str, tuple[int, str]]
) -> tuple[CommitmentCheckScheduler, list[list[str]]]:
    """A scheduler whose systemctl verbs answer ``outcomes`` by verb name; every other command succeeds."""
    calls: list[list[str]] = []

    def run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(command))
        code, stderr = next(
            (v for verb, v in outcomes.items() if verb in command), (0, "")
        )
        return subprocess.CompletedProcess(list(command), code, "", stderr)

    rules = ModelCommitmentCheckRules.model_validate(
        yaml.safe_load(_EXAMPLE.read_text(encoding="utf-8"))
    )
    scheduler = CommitmentCheckScheduler(
        schedule=load_schedule(), overlay=rules, home=tmp_path / "home", run=run
    )
    return scheduler, calls


def _installed(
    tmp_path: Path, outcomes: dict[str, tuple[int, str]]
) -> tuple[CommitmentCheckScheduler, list[list[str]], Path]:
    scheduler, calls = _scheduler(tmp_path, {})
    scheduler.install(_HOST, omni_root=tmp_path / "root", overlay_root=tmp_path / "ovl")
    scheduler, calls = _scheduler(tmp_path, outcomes)
    return scheduler, calls, tmp_path / "home/.config/systemd/user"


def test_5347_11_a_stop_that_failed_is_reported_and_keeps_the_unit_files(
    tmp_path: Path,
) -> None:
    scheduler, _calls, unit_dir = _installed(
        tmp_path, {"disable": (1, "Failed to disable unit: Access denied")}
    )
    result = scheduler.uninstall(_HOST)
    assert result.exit_code != 0
    assert "Access denied" in result.stderr
    assert len(list(unit_dir.iterdir())) == 2


def test_5347_11_a_timer_that_was_never_installed_is_not_a_stop_failure(
    tmp_path: Path,
) -> None:
    scheduler, _calls, _unit_dir = _installed(
        tmp_path,
        {
            "disable": (
                1,
                "Failed to disable unit: Unit file onex-commitment-check.timer does not exist.",
            )
        },
    )
    assert scheduler.uninstall(_HOST).exit_code == 0


def test_5347_11_a_daemon_reload_that_failed_after_the_stop_is_reported(
    tmp_path: Path,
) -> None:
    scheduler, _calls, _unit_dir = _installed(
        tmp_path, {"daemon-reload": (1, "Failed to connect to bus")}
    )
    result = scheduler.uninstall(_HOST)
    assert result.exit_code != 0
    assert "Failed to connect to bus" in result.stderr


def test_5347_12_a_relative_workspace_root_is_refused_not_rendered_into_the_unit(
    tmp_path: Path,
) -> None:
    scheduler, _calls = _scheduler(tmp_path, {})
    result = scheduler.install(
        _HOST, omni_root=Path("."), overlay_root=tmp_path / "ovl", dry_run=True
    )
    assert result.exit_code == 1
    assert "absolute" in result.stderr
