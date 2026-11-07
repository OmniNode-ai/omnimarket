# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Six schedule installers refuse a unit that runs from a worktree (OMN-22767).

Recorded case: on 2026-11-06 the host worktree reconcile removed the tree an installed reading writer
unit ran from, and every tick of that unit failed (OMN-22735 closed that one installer). Six loaded units
on the same host still named a worktree tree for their interpreter, their working directory or their
ledger command, and six other installers rendered and wrote such a unit without a word. Each installer
now reads its rendered service and timer text through the shared guard before it writes.

Every case builds the installer from the node's own example overlay, points its interpreter at a path
under ``omni_worktrees`` and installs. The failing control lets the guard through by name and the same
install writes the worktree unit.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.handlers.node_overlay_reader import OVERLAY_ROOTS_ENV
from omnimarket.models.model_host_reconcile_schedule import (
    ModelHostReconcileHostRow,
    ModelHostReconcileScheduleOverlay,
)
from omnimarket.models.model_lane_census import ModelLaneCensusScheduleOverlay
from omnimarket.nodes.node_code_graph_effect.handlers import (
    schedule as code_graph_module,
)
from omnimarket.nodes.node_code_graph_effect.handlers.schedule import CodeGraphScheduler
from omnimarket.nodes.node_code_graph_effect.overlay import ModelCodeGraphOverlay
from omnimarket.nodes.node_consumer_group_guard_effect import (
    guard_schedule as guard_module,
)
from omnimarket.nodes.node_consumer_group_guard_effect.guard_schedule import (
    GuardScheduler,
)
from omnimarket.nodes.node_consumer_group_guard_effect.guard_schedule_models import (
    ModelGuardHostRow,
    ModelGuardScheduleOverlay,
)
from omnimarket.nodes.node_host_reading_effect.handlers import (
    schedule as host_reading_module,
)
from omnimarket.nodes.node_host_reading_effect.handlers.schedule import (
    HostReadingScheduler,
)
from omnimarket.nodes.node_host_reading_effect.overlay import ModelHostReadingOverlay
from omnimarket.nodes.node_host_reconcile_effect.handlers import (
    adapter_host_reconcile_schedule as host_reconcile_module,
)
from omnimarket.nodes.node_host_reconcile_effect.handlers.adapter_host_reconcile_schedule import (
    HostReconcileScheduler,
)
from omnimarket.nodes.node_lane_census_effect.handlers import (
    adapter_lane_census_schedule as lane_census_module,
)
from omnimarket.nodes.node_lane_census_effect.handlers.adapter_lane_census_schedule import (
    LaneCensusScheduler,
)
from omnimarket.nodes.node_overlay_drift_effect.handlers import (
    schedule as overlay_drift_module,
)
from omnimarket.nodes.node_overlay_drift_effect.handlers.schedule import DriftScheduler
from omnimarket.nodes.node_overlay_drift_effect.overlay import ModelOverlayDriftOverlay

pytestmark = pytest.mark.unit

NODES = Path(code_graph_module.__file__).resolve().parents[2]
WORKTREE = "omni_worktrees/OMN-1/clone"
STABLE = "stable-clone"


def _ok(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """A systemctl that accepts everything and lists every timer the installers check for."""
    listed = (
        "NEXT LEFT onex-host-reconcile.timer onex-consumer-group-guard.timer\n"
        if "list-timers" in command
        else ""
    )
    return subprocess.CompletedProcess(list(command), 0, listed, "")


def _example(node: str) -> dict[str, Any]:
    document = yaml.safe_load(
        (NODES / node / "overlay.example.yaml").read_text(encoding="utf-8")
    )
    assert isinstance(document, dict)
    return document


def _interpreter(root: Path, where: str, venv_name: str) -> None:
    python = root / where / venv_name / "bin" / "python"
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_text("#!/bin/sh\n")


@dataclass(frozen=True)
class Installer:
    """One installer under test: its module, and a call that installs under a chosen root."""

    name: str
    module: Any
    install: Callable[[Path, str, bool], Any]


def _code_graph(tmp_path: Path, where: str, dry_run: bool) -> Any:
    document = _example("node_code_graph_effect")
    document["schedule_hosts"] = [
        {"name": "h-test", "scheduler": "systemd-user", "venv": f"{where}/.venv"}
    ]
    root = tmp_path / "registry"
    _interpreter(root, where, ".venv")
    return CodeGraphScheduler(
        schedule=code_graph_module.load_schedule(),
        overlay=ModelCodeGraphOverlay.model_validate(document),
        home=tmp_path / "home",
        run=_ok,
    ).install(
        "h-test", omni_root=root, overlay_root=tmp_path / "overlays", dry_run=dry_run
    )


def _host_reading(tmp_path: Path, where: str, dry_run: bool) -> Any:
    document = _example("node_host_reading_effect")
    for host in document["hosts"]:
        if host["scheduler"] == "systemd-user":
            host["venv"] = f"{where}/.venv"
    root = tmp_path / "registry"
    _interpreter(root, where, ".venv")
    return HostReadingScheduler(
        schedule=host_reading_module.load_schedule(),
        overlay=ModelHostReadingOverlay.model_validate(document),
        home=tmp_path / "home",
        run=_ok,
        uid=501,
    ).install(
        "example-writing-host",
        omni_root=root,
        overlay_root=tmp_path / "overlays",
        dry_run=dry_run,
    )


def _overlay_drift(tmp_path: Path, where: str, dry_run: bool) -> Any:
    document = _example("node_overlay_drift_effect")
    document["schedule_hosts"] = [
        {"name": "h-test", "scheduler": "systemd-user", "venv": f"{where}/.venv"}
    ]
    root = tmp_path / "registry"
    _interpreter(root, where, ".venv")
    return DriftScheduler(
        schedule=overlay_drift_module.load_schedule(),
        overlay=ModelOverlayDriftOverlay.model_validate(document),
        home=tmp_path / "home",
        run=_ok,
    ).install(
        "h-test", omni_root=root, overlay_root=tmp_path / "overlays", dry_run=dry_run
    )


def _lane_census(tmp_path: Path, where: str, dry_run: bool) -> Any:
    root = tmp_path / "registry"
    _interpreter(root, where, ".venv")
    overlay_root = tmp_path / "overlays"
    target = overlay_root / lane_census_module.NODE_NAME / "overlay.yaml"
    target.parent.mkdir(parents=True)
    document = _example("node_lane_census_effect")
    target.write_text(yaml.safe_dump(document), encoding="utf-8")
    return LaneCensusScheduler(
        schedule=lane_census_module.load_schedule(),
        overlay=ModelLaneCensusScheduleOverlay.model_validate(document),
        home=tmp_path / "home",
        dispatch_venv=root / where / ".venv",
        run=_ok,
        environ={OVERLAY_ROOTS_ENV: str(overlay_root)},
    ).install("host-a", omni_root=root, dry_run=dry_run)


def _host_reconcile(tmp_path: Path, where: str, dry_run: bool) -> Any:
    root = tmp_path / "registry"
    _interpreter(root, where, ".venv")
    return HostReconcileScheduler(
        schedule=host_reconcile_module.load_schedule(),
        overlay=ModelHostReconcileScheduleOverlay(
            hosts=(
                ModelHostReconcileHostRow.model_validate(
                    {"name": "h-test", "scheduler": "systemd-user"}
                ),
            )
        ),
        home=tmp_path / "home",
        dispatch_venv=root / where / ".venv",
        run=_ok,
    ).install("h-test", omni_root=root, dry_run=dry_run)


def _consumer_group_guard(tmp_path: Path, where: str, dry_run: bool) -> Any:
    root = tmp_path / "registry"
    _interpreter(root, where, ".venv")
    return GuardScheduler(
        schedule=guard_module.load_schedule(),
        overlay=ModelGuardScheduleOverlay(
            hosts=(
                ModelGuardHostRow(
                    name="h-test",
                    scheduler="systemd-user",
                    bus_lane="dev",
                    workspace_config_root="/srv/fixture/config",
                ),
            )
        ),
        home=tmp_path / "home",
        dispatch_venv=root / where / ".venv",
        run=_ok,
    ).install("h-test", omni_root=root, dry_run=dry_run)


INSTALLERS = [
    Installer("code_graph", code_graph_module, _code_graph),
    Installer("host_reading", host_reading_module, _host_reading),
    Installer("overlay_drift", overlay_drift_module, _overlay_drift),
    Installer("lane_census", lane_census_module, _lane_census),
    Installer("host_reconcile", host_reconcile_module, _host_reconcile),
    Installer("consumer_group_guard", guard_module, _consumer_group_guard),
]
IDS = [case.name for case in INSTALLERS]


def _written(tmp_path: Path) -> list[Path]:
    home = tmp_path / "home"
    return sorted(home.rglob("*")) if home.exists() else []


@pytest.mark.parametrize("case", INSTALLERS, ids=IDS)
def test_a_unit_rooted_in_a_worktree_is_refused_and_nothing_is_written(
    case: Installer, tmp_path: Path
) -> None:
    result = case.install(tmp_path, WORKTREE, False)
    assert result.exit_code == 1
    assert WORKTREE in result.stderr
    assert "omni_worktrees" in result.stderr
    assert not [path for path in _written(tmp_path) if path.is_file()]


@pytest.mark.parametrize("case", INSTALLERS, ids=IDS)
def test_a_dry_run_refuses_a_worktree_root_too(case: Installer, tmp_path: Path) -> None:
    result = case.install(tmp_path, WORKTREE, True)
    assert result.exit_code == 1
    assert WORKTREE in result.stderr
    assert "[Timer]" not in result.stdout


@pytest.mark.parametrize("case", INSTALLERS, ids=IDS)
def test_a_stable_root_installs_and_writes_no_worktree_text(
    case: Installer, tmp_path: Path
) -> None:
    result = case.install(tmp_path, STABLE, False)
    assert result.exit_code == 0, result.stderr
    files = [path for path in _written(tmp_path) if path.is_file()]
    assert files, "positive control: the install wrote its unit files"
    for path in files:
        assert "omni_worktrees" not in path.read_bytes().decode("utf-8")


@pytest.mark.parametrize("case", INSTALLERS, ids=IDS)
def test_with_the_guard_let_through_the_same_install_writes_a_worktree_unit(
    case: Installer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The named injected break: the failing control of the refusal tests above."""
    monkeypatch.setattr(case.module, "refuse_worktree_roots", lambda _units: None)
    result = case.install(tmp_path, WORKTREE, False)
    assert result.exit_code == 0, result.stderr
    texts = [
        path.read_bytes().decode("utf-8")
        for path in _written(tmp_path)
        if path.is_file()
    ]
    assert any("omni_worktrees" in text for text in texts)
