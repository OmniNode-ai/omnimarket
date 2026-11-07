# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The central test harness's own timer (OMN-22893).

Golden: a declared host gets the two unit files and an enabled timer whose interval is the overlay's, and the
rollback removes them. Error: an undeclared host, a missing interpreter, a runtime that cannot be imported and a unit
root inside a worktree each refuse before any unit file is written.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

from omnimarket.models.lab_test_harness import ModelLabTestHarnessOverlay
from omnimarket.nodes.node_lab_test_harness_effect.handlers.schedule import (
    RUNTIME_MODULE,
    HarnessScheduler,
    load_unit,
    render_units,
)
from tests.helpers.lab_test_harness_fakes import overlay_document

pytestmark = pytest.mark.unit

SHA = "a" * 40


def _overlay(**extra: object) -> ModelLabTestHarnessOverlay:
    document = overlay_document(SHA, Path("/tmp/reading.json"))
    document["schedule"] = {"workflow": "lab-test-harness", "interval_seconds": 1800}
    document["schedule_hosts"] = [{"name": "h053", "venv": ".venv"}]
    document["unit_environment"] = {
        "ONEX_STATE_DIR": "$OMNI_HOME/.onex_state",
        "ONEX_LANE_HOME": "$HOME/.onex",
    }
    document.update(extra)
    return ModelLabTestHarnessOverlay.model_validate(document)


class _Systemctl:
    """Records every command; the interpreter import check can be made to fail."""

    def __init__(self, *, import_exit: int = 0) -> None:
        self.commands: list[list[str]] = []
        self.import_exit = import_exit

    def __call__(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.commands.append(list(command))
        if command[1:2] == ["-c"]:
            return subprocess.CompletedProcess(
                list(command), self.import_exit, "", "no"
            )
        return subprocess.CompletedProcess(list(command), 0, "", "")


def _tree(tmp_path: Path) -> tuple[Path, Path, Path]:
    omni_root = tmp_path / "registry"
    (omni_root / ".venv/bin").mkdir(parents=True)
    # A launcher, not a symlink: the interpreter finds its site-packages from the path it was started by.
    launcher = omni_root / ".venv/bin/python"
    launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
    launcher.chmod(0o755)
    home = tmp_path / "user"
    home.mkdir()
    overlay_root = tmp_path / "overlay-root"
    overlay_root.mkdir()
    return omni_root, home, overlay_root


def test_render_takes_the_interval_and_environment_from_the_overlay(
    tmp_path: Path,
) -> None:
    omni_root, home, overlay_root = _tree(tmp_path)
    service, timer = render_units(
        load_unit(),
        _overlay(),
        omni_root=omni_root,
        venv=omni_root / ".venv",
        home=home,
        overlay_root=overlay_root,
    )
    assert "OnUnitActiveSec=1800s" in timer
    assert f"-m {RUNTIME_MODULE} --omni-home" in service
    assert f'Environment="ONEX_STATE_DIR={omni_root}/.onex_state"' in service
    assert f'Environment="ONEX_LANE_HOME={home}/.onex"' in service
    assert f'Environment="ONEX_SKILL_OVERLAY_ROOTS={overlay_root}"' in service


def test_golden_install_enables_the_timer_and_uninstall_is_the_rollback(
    tmp_path: Path,
) -> None:
    omni_root, home, overlay_root = _tree(tmp_path)
    runner = _Systemctl()
    scheduler = HarnessScheduler(
        unit=load_unit(), overlay=_overlay(), home=home, run=runner
    )
    done = scheduler.install("h053", omni_root=omni_root, overlay_root=overlay_root)
    assert done.exit_code == 0, done.stderr
    unit_dir = home / ".config/systemd/user"
    assert (unit_dir / "onex-central-test-harness.service").is_file()
    assert (unit_dir / "onex-central-test-harness.timer").is_file()
    assert [
        "systemctl",
        "--user",
        "enable",
        "--now",
        "onex-central-test-harness.timer",
    ] in runner.commands
    again = scheduler.install("h053", omni_root=omni_root, overlay_root=overlay_root)
    assert "unchanged" in again.stdout
    gone = scheduler.uninstall("h053")
    assert gone.exit_code == 0
    assert not (unit_dir / "onex-central-test-harness.service").exists()
    assert not (unit_dir / "onex-central-test-harness.timer").exists()
    assert [
        "systemctl",
        "--user",
        "disable",
        "--now",
        "onex-central-test-harness.timer",
    ] in runner.commands


def test_golden_installed_service_runs_the_runtime_import_for_real(
    tmp_path: Path,
) -> None:
    omni_root, home, overlay_root = _tree(tmp_path)
    scheduler = HarnessScheduler(
        unit=load_unit(),
        overlay=_overlay(),
        home=home,
        run=lambda command: subprocess.run(
            list(command) if command[0] != "systemctl" else ["true"],
            capture_output=True,
            text=True,
            check=False,
        ),
    )
    done = scheduler.install("h053", omni_root=omni_root, overlay_root=overlay_root)
    assert done.exit_code == 0, done.stderr


def test_error_undeclared_host_writes_nothing(tmp_path: Path) -> None:
    omni_root, home, overlay_root = _tree(tmp_path)
    scheduler = HarnessScheduler(
        unit=load_unit(), overlay=_overlay(), home=home, run=_Systemctl()
    )
    done = scheduler.install("h999", omni_root=omni_root, overlay_root=overlay_root)
    assert done.exit_code == 1
    assert (
        "not in the node_lab_test_harness_effect overlay's schedule_hosts"
        in done.stderr
    )
    assert not (home / ".config").exists()
    assert scheduler.uninstall("h999").exit_code == 1


def test_error_no_declared_host_fails_the_check(tmp_path: Path) -> None:
    scheduler = HarnessScheduler(
        unit=load_unit(),
        overlay=_overlay(schedule_hosts=[]),
        home=tmp_path,
        run=_Systemctl(),
    )
    assert scheduler.check().exit_code == 1


def test_error_missing_interpreter_writes_nothing(tmp_path: Path) -> None:
    omni_root, home, overlay_root = _tree(tmp_path)
    (omni_root / ".venv/bin/python").unlink()
    scheduler = HarnessScheduler(
        unit=load_unit(), overlay=_overlay(), home=home, run=_Systemctl()
    )
    done = scheduler.install("h053", omni_root=omni_root, overlay_root=overlay_root)
    assert done.exit_code == 1
    assert "no venv interpreter" in done.stderr
    assert not (home / ".config").exists()


def test_error_injected_import_break_writes_nothing(tmp_path: Path) -> None:
    """The named break: the runtime cannot be imported, so the run would not start."""
    omni_root, home, overlay_root = _tree(tmp_path)
    scheduler = HarnessScheduler(
        unit=load_unit(), overlay=_overlay(), home=home, run=_Systemctl(import_exit=1)
    )
    done = scheduler.install("h053", omni_root=omni_root, overlay_root=overlay_root)
    assert done.exit_code == 1
    assert "the harness run would not start" in done.stderr
    assert not (home / ".config").exists()


def test_error_unit_root_in_a_worktree_is_refused(tmp_path: Path) -> None:
    omni_root, home, _overlay_root = _tree(tmp_path)
    worktree_root = tmp_path / "omni_worktrees" / "OMN-1" / "overlays"
    worktree_root.mkdir(parents=True)
    scheduler = HarnessScheduler(
        unit=load_unit(), overlay=_overlay(), home=home, run=_Systemctl()
    )
    done = scheduler.install("h053", omni_root=omni_root, overlay_root=worktree_root)
    assert done.exit_code == 1
    assert not (home / ".config").exists()
