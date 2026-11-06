# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seam deadline, work ledger copy and worktree reconcile installs refuse a worktree root (OMN-22783).

OMN-22735 closed the reading writer's install and OMN-22767 closed six more. The same recorded case
(on 2026-11-06 the host worktree reconcile removed the tree an installed unit ran from, and every tick of
that unit failed) is still open for three installers on dev: the hourly seam deadline unit, the work
ledger copy's launchd agent and the worktree reconcile's own schedule. Each now reads what it renders
through the shared guard before it writes.

Every case builds the installer from the node's own example overlay or a fixture of the same shape, points
its interpreter, working directory or ledger command at a path under ``omni_worktrees`` and installs. The
failing control lets the guard through by name and the same install writes the worktree unit.
"""

from __future__ import annotations

import json
import plistlib
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml

from omnimarket.events.worktree_reconcile import ModelWorktreeReconcileCommand
from omnimarket.handlers.rules_unit_roots import (
    UnitRootRefusedError,
    plist_paths,
    refuse_worktree_plists,
)
from omnimarket.nodes.node_seam_deadline_probe_effect.handlers import (
    schedule as seam_module,
)
from omnimarket.nodes.node_seam_deadline_probe_effect.handlers.schedule import (
    SeamDeadlineScheduler,
)
from omnimarket.nodes.node_seam_deadline_probe_effect.overlay import (
    ModelSeamDeadlineOverlay,
)
from omnimarket.nodes.node_work_ledger_copy_effect import schedule as copy_module
from omnimarket.nodes.node_work_ledger_copy_effect.models import ModelCopyOverlay
from omnimarket.nodes.node_work_ledger_copy_effect.runtime_work_ledger_copy import (
    _schedule as copy_schedule_verb,
)
from omnimarket.nodes.node_worktree_reconcile_effect.handlers import (
    handler_worktree_schedule as reconcile_module,
)
from omnimarket.nodes.node_worktree_reconcile_effect.handlers.handler_worktree_schedule import (
    HandlerWorktreeSchedule,
)
from omnimarket.nodes.node_worktree_reconcile_effect.models.model_worktree_schedule import (
    ModelWorktreeScheduleRequest,
)

pytestmark = pytest.mark.unit

NODES = Path(seam_module.__file__).resolve().parents[2]
WORKTREE = "omni_worktrees/OMN-1/clone"
STABLE = "stable-clone"
WORKTREE_LEDGER_COMMAND = "/srv/omni_worktrees/OMN-1/omnimarket/.venv/bin/python -m omnimarket.work_ledger_bus.cli append"


def _ok(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """A systemctl or launchctl that accepts everything and lists the reconcile timer."""
    listed = "onex-worktree-reconcile.timer\n" if "list-timers" in command else ""
    return subprocess.CompletedProcess(list(command), 0, listed, "")


def _example(node: str) -> dict[str, Any]:
    document = yaml.safe_load(
        (NODES / node / "overlay.example.yaml").read_text(encoding="utf-8")
    )
    assert isinstance(document, dict)
    return document


@dataclass(frozen=True)
class Installer:
    """One installer under test: the module that holds its guard, and a call that installs."""

    name: str
    module: Any
    guard: str
    install: Callable[[Path, str], Any]
    dry_run: Callable[[Path, str], Any] | None = None


def _written(tmp_path: Path) -> list[Path]:
    files = [
        path
        for root in (tmp_path / "home", tmp_path / "agents", tmp_path / "units")
        if root.exists()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    ]
    return files


def _seam(
    tmp_path: Path, where: str, dry_run: bool, ledger_command: str | None = None
) -> Any:
    document = _example("node_seam_deadline_probe_effect")
    if ledger_command is not None:
        document["schedule_hosts"][0]["environment"][
            "EXAMPLE_LEDGER_APPEND_COMMAND"
        ] = ledger_command
    root = tmp_path / where
    python = root / "example-venv" / "bin" / "python"
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_text("#!/bin/sh\n")
    return SeamDeadlineScheduler(
        schedule=seam_module.load_schedule(),
        overlay=ModelSeamDeadlineOverlay.model_validate(document),
        home=tmp_path / "home",
        run=_ok,
    ).install(
        "example-lab-host",
        omni_root=root,
        overlay_root=tmp_path / "overlays",
        dry_run=dry_run,
    )


def _copy_overlay(where: str, environment: dict[str, str] | None = None) -> Any:
    document = _example("node_work_ledger_copy_effect")
    document["launchd"]["interpreter"] = f"/srv/{where}/.venv/bin/python"
    document["launchd"]["working_directory"] = f"/srv/{where}"
    if environment is not None:
        document["launchd"]["environment"].update(environment)
    return ModelCopyOverlay.model_validate(document)


def _copy_install(tmp_path: Path, where: str) -> int:
    return copy_module.install(
        _copy_overlay(where),
        copy_module.load_schedule(),
        agents_dir=tmp_path / "agents",
        uid=501,
        run=_ok,
    )


def _reconcile(tmp_path: Path, where: str, mode: str) -> Any:
    project = tmp_path / where
    entry = project / "src/omnimarket/nodes/node_worktree_reconcile_effect/__main__.py"
    entry.parent.mkdir(parents=True)
    entry.write_text("# fixture entry\n")
    for argv in (
        ["git", "init", "--quiet", str(project)],
        ["git", "-C", str(project), "add", "src"],
        [
            "git",
            "-C",
            str(project),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "fixture",
        ],
    ):
        subprocess.run(argv, check=True, capture_output=True, text=True)
    python = tmp_path / "python3"
    python.write_text("#!/bin/sh\n")
    environment = tmp_path / "host.env"
    environment.write_text("KAFKA_BROKERS=localhost:9092\n")
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text(
        yaml.safe_dump(
            {
                "python": str(python),
                "project": str(project),
                "state_directory": str(tmp_path / "state"),
                "environment_file": str(environment),
                "hosts": {
                    "lab-host": json.loads(
                        ModelWorktreeReconcileCommand(
                            host="lab-host",
                            roots=(str(tmp_path / "trees"),),
                            correlation_id=uuid4(),
                        ).model_dump_json()
                    )
                },
            }
        )
    )
    return HandlerWorktreeSchedule(
        unit_directory=tmp_path / "units", runner=_ok
    ).handle(
        ModelWorktreeScheduleRequest(
            mode=mode,  # type: ignore[arg-type]
            host="lab-host",
            overlay_path=str(overlay),
        )
    )


INSTALLERS = [
    Installer(
        "seam_deadline",
        seam_module,
        "refuse_worktree_roots",
        lambda tmp, where: _seam(tmp, where, False),
        lambda tmp, where: _seam(tmp, where, True),
    ),
    Installer(
        "work_ledger_copy",
        copy_module,
        "refuse_worktree_plists",
        lambda tmp, where: _copy_install(tmp, where),
    ),
    Installer(
        "worktree_reconcile",
        reconcile_module,
        "refuse_worktree_roots",
        lambda tmp, where: _reconcile(tmp, where, "install"),
        lambda tmp, where: _reconcile(tmp, where, "render"),
    ),
]
IDS = [case.name for case in INSTALLERS]
DRY_RUNS = [case for case in INSTALLERS if case.dry_run is not None]


def _failed(result: Any) -> tuple[bool, str]:
    """Whether the install refused, and the text it refused with, for an int or a typed result."""
    if isinstance(result, int):
        return result != 0, ""
    return result.exit_code != 0, result.stderr


@pytest.mark.parametrize("case", INSTALLERS, ids=IDS)
def test_a_unit_rooted_in_a_worktree_is_refused_and_nothing_is_written(
    case: Installer, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    if case.name == "work_ledger_copy":
        with pytest.raises(UnitRootRefusedError, match="omni_worktrees"):
            case.install(tmp_path, WORKTREE)
        assert not _written(tmp_path)
        return
    result = case.install(tmp_path, WORKTREE)
    refused, stderr = _failed(result)
    assert refused
    assert WORKTREE in stderr
    assert "omni_worktrees" in stderr
    assert not [path for path in _written(tmp_path) if path.name != "overlay.yaml"]


@pytest.mark.parametrize("case", DRY_RUNS, ids=[case.name for case in DRY_RUNS])
def test_a_dry_run_or_render_refuses_a_worktree_root_too(
    case: Installer, tmp_path: Path
) -> None:
    assert case.dry_run is not None
    result = case.dry_run(tmp_path, WORKTREE)
    assert result.exit_code != 0
    assert WORKTREE in result.stderr
    assert "[Timer]" not in result.stdout
    assert not getattr(result, "service", "")


@pytest.mark.parametrize("case", INSTALLERS, ids=IDS)
def test_a_stable_root_installs_and_writes_no_worktree_text(
    case: Installer, tmp_path: Path
) -> None:
    result = case.install(tmp_path, STABLE)
    failed, stderr = _failed(result)
    assert not failed, stderr
    files = _written(tmp_path)
    assert files, "positive control: the install wrote its unit files"
    for path in files:
        assert "omni_worktrees" not in path.read_bytes().decode("utf-8")


@pytest.mark.parametrize("case", INSTALLERS, ids=IDS)
def test_with_the_guard_let_through_the_same_install_writes_a_worktree_unit(
    case: Installer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The named injected break: the failing control of the refusal tests above."""
    monkeypatch.setattr(case.module, case.guard, lambda _units: None)
    result = case.install(tmp_path, WORKTREE)
    failed, stderr = _failed(result)
    assert not failed, stderr
    texts = [path.read_bytes().decode("utf-8") for path in _written(tmp_path)]
    assert any("omni_worktrees" in text for text in texts)


def test_a_seam_deadline_ledger_command_under_a_worktree_is_refused(
    tmp_path: Path,
) -> None:
    result = _seam(tmp_path, STABLE, False, ledger_command=WORKTREE_LEDGER_COMMAND)
    assert result.exit_code == 1
    assert "/srv/omni_worktrees/OMN-1/omnimarket/.venv/bin/python" in result.stderr
    assert not _written(tmp_path)


def test_a_work_ledger_copy_environment_path_under_a_worktree_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    overlay = _copy_overlay(
        STABLE, {"ONEX_SKILL_OVERLAY_ROOTS": "/srv/omni_worktrees/OMN-1/overlays"}
    )
    with pytest.raises(
        UnitRootRefusedError, match="/srv/omni_worktrees/OMN-1/overlays"
    ):
        copy_module.install(
            overlay,
            copy_module.load_schedule(),
            agents_dir=tmp_path / "agents",
            uid=501,
            run=_ok,
        )
    assert not _written(tmp_path)


def test_the_work_ledger_copy_verbs_name_the_refusal_and_exit_one(
    capsys: pytest.CaptureFixture[str],
) -> None:
    overlay = _copy_overlay(WORKTREE)
    for verb in ("schedule-render", "schedule-install"):
        assert copy_schedule_verb(verb, overlay) == 1
        captured = capsys.readouterr()
        assert "omni_worktrees" in captured.err
        assert captured.out == ""


def _plist(**fields: Any) -> bytes:
    return plistlib.dumps(fields)


def test_the_plist_guard_reads_program_working_directory_and_environment() -> None:
    data = _plist(
        ProgramArguments=["/opt/venv/bin/python", "-m", "node", "run"],
        WorkingDirectory="/srv/omni_worktrees/OMN-1/clone",
        EnvironmentVariables={"PATH": "/usr/bin:/bin", "ROOT": "/srv/registry"},
    )
    assert plist_paths(data) == {
        "/opt/venv/bin/python",
        "/srv/omni_worktrees/OMN-1/clone",
        "/usr/bin",
        "/bin",
        "/srv/registry",
    }
    with pytest.raises(UnitRootRefusedError) as raised:
        refuse_worktree_plists({"agent.plist": data})
    assert raised.value.paths == ("/srv/omni_worktrees/OMN-1/clone",)


def test_the_plist_guard_leaves_a_log_path_and_a_stable_plist_alone() -> None:
    data = _plist(
        ProgramArguments=["/opt/venv/bin/python", "-m", "node", "run"],
        WorkingDirectory="/srv/registry",
        StandardOutPath="/srv/omni_worktrees/OMN-1/logs/out.log",
        StandardErrorPath="/srv/omni_worktrees/OMN-1/logs/err.log",
    )
    refuse_worktree_plists({"agent.plist": data})
