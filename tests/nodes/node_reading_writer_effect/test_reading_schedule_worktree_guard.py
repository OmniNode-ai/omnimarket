# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The schedule install refuses a unit that runs from a worktree (OMN-22735).

Recorded case: on 2026-11-06 the overlay-driven install of the hourly reading writer accepted a venv and
two ledger commands under omni_worktrees/OMN-22643, the host reconcile removed that tree at 18:16:28Z, and
the 18:15Z tick failed every piece. The recorded unit text is the fixture the node's reconcile tests use.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.handlers.rules_unit_roots import (
    UnitRootRefusedError,
    refuse_worktree_roots,
    worktree_paths,
)
from omnimarket.nodes.node_reading_writer_effect.handlers import (
    schedule as schedule_module,
)
from omnimarket.nodes.node_reading_writer_effect.handlers.schedule import (
    ReadingScheduler,
    load_schedule,
)
from omnimarket.nodes.node_reading_writer_effect.overlay import ModelReadingOverlay
from omnimarket.nodes.node_reading_writer_effect.runtime_reading_writer import main
from tests.nodes.node_reading_writer_effect.reading_support import overlay_document

pytestmark = pytest.mark.unit

FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "worktree_reconcile"
    / "onex-reading-hourly.service.18-15Z.txt"
)
TREE = "/srv/registry/omni_worktrees/OMN-22643/omnimarket"


def _ok(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(list(command), 0, "", "")


def _install(
    tmp_path: Path,
    *,
    venv: str = "venv-readings",
    environment: dict[str, str] | None = None,
    dry_run: bool = False,
) -> tuple[Any, Path]:
    root = tmp_path / "registry"
    python = root / venv / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\n")
    home = tmp_path / "home"
    home.mkdir()
    document = overlay_document()
    document["schedule_hosts"] = [
        {"name": "h-test", "scheduler": "systemd-user", "venv": venv}
    ]
    if environment is not None:
        document["unit_environment"] = {
            **document.get("unit_environment", {}),
            **environment,
        }
    scheduler = ReadingScheduler(
        schedule=load_schedule(),
        overlay=ModelReadingOverlay.model_validate(document),
        home=home,
        run=_ok,
    )
    result = scheduler.install(
        "h-test", omni_root=root, overlay_root=tmp_path / "overlays", dry_run=dry_run
    )
    return result, home / ".config"


def test_the_recorded_unit_names_three_worktree_paths_and_is_refused() -> None:
    text = FIXTURE.read_text(encoding="utf-8")
    assert worktree_paths(text) == (
        "/srv/registry/omni_worktrees/OMN-22643/omnimarket/.venv/bin/onex",
        "/srv/registry/omni_worktrees/OMN-22643/omnimarket/.venv/bin/python",
    )
    with pytest.raises(UnitRootRefusedError) as refused:
        refuse_worktree_roots({"onex-reading-hourly.service": text})
    assert refused.value.unit == "onex-reading-hourly.service"
    assert TREE + "/.venv/bin/python" in str(refused.value)


def test_a_venv_under_a_worktree_is_refused_and_nothing_is_written(
    tmp_path: Path,
) -> None:
    result, config = _install(tmp_path, venv="omni_worktrees/OMN-1/clone/.venv")
    assert result.exit_code == 1
    assert "omni_worktrees/OMN-1/clone/.venv/bin/python" in result.stderr
    assert not config.exists()


def test_a_ledger_command_under_a_worktree_is_refused_though_the_venv_is_stable(
    tmp_path: Path,
) -> None:
    command = f"{tmp_path}/registry/omni_worktrees/OMN-2/clone/.venv/bin/onex ledger query --stdin"
    result, config = _install(tmp_path, environment={"READING_TEST_APPEND": command})
    assert result.exit_code == 1
    assert "omni_worktrees/OMN-2/clone" in result.stderr
    assert not config.exists()


def test_a_dry_run_refuses_too(tmp_path: Path) -> None:
    result, _ = _install(
        tmp_path, venv="omni_worktrees/OMN-3/clone/.venv", dry_run=True
    )
    assert result.exit_code == 1
    assert "omni_worktrees/OMN-3" in result.stderr
    assert "[Timer]" not in result.stdout


def test_a_stable_root_installs_and_writes_both_units(tmp_path: Path) -> None:
    result, config = _install(tmp_path)
    assert result.exit_code == 0, result.stderr
    for name in ("onex-reading-hourly.service", "onex-reading-hourly.timer"):
        text = (config / "systemd/user" / name).read_text(encoding="utf-8")
        assert "omni_worktrees" not in text


def test_with_the_guard_let_through_the_same_install_writes_a_worktree_unit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The named injected break: the failing control of the refusal tests above."""
    monkeypatch.setattr(schedule_module, "refuse_worktree_roots", lambda _units: None)
    result, config = _install(tmp_path, venv="omni_worktrees/OMN-1/clone/.venv")
    assert result.exit_code == 0
    service = (config / "systemd/user/onex-reading-hourly.service").read_text()
    assert "omni_worktrees/OMN-1/clone/.venv/bin/python" in service


def _cli_overlay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, venv: str) -> Path:
    document = overlay_document()
    document["schedule_hosts"] = [
        {"name": "h-test", "scheduler": "systemd-user", "venv": venv}
    ]
    root = tmp_path / "overlays"
    (root / "node_reading_writer_effect").mkdir(parents=True)
    (root / "node_reading_writer_effect" / "overlay.yaml").write_text(
        yaml.safe_dump(document), encoding="utf-8"
    )
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(root))
    registry = tmp_path / "registry"
    registry.mkdir()
    return registry


def test_the_schedule_cli_exits_one_naming_the_path_for_a_worktree_venv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    registry = _cli_overlay(tmp_path, monkeypatch, "omni_worktrees/OMN-4/clone/.venv")
    code = main(
        ["schedule", "install", "--host", "h-test", "--workspace-root", str(registry)]
    )
    err = capsys.readouterr().err
    assert code == 1
    assert "omni_worktrees/OMN-4/clone/.venv/bin/python" in err


def test_the_schedule_cli_dry_run_passes_for_a_stable_venv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    registry = _cli_overlay(tmp_path, monkeypatch, "venv-readings")
    code = main(
        [
            "schedule",
            "install",
            "--host",
            "h-test",
            "--workspace-root",
            str(registry),
            "--dry-run",
        ]
    )
    assert code == 0
    assert "omni_worktrees" not in capsys.readouterr().out
