# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The last three schedule installers refuse a unit that runs from a worktree (OMN-22783).

After the reading writer (OMN-22735) and six more installers (OMN-22767), three installers on dev
still rendered and wrote a unit whose program, working directory or environment named a path under
``omni_worktrees``: the hourly seam deadline unit (systemd), the work ledger copy's launchd agent and
the worktree reconcile's own schedule. The host worktree reconcile removes a tree the tick after its
work reads merged, so a unit installed from one dies.

Every case builds the installer from its node's example overlay, points one path-bearing part at a
path under ``omni_worktrees`` (the program, the working directory or the environment) and installs, in
a dry run or render as well. The failing control lets the guard through by name and the same install
writes the worktree unit.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

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
VECTORS = ("program", "workdir", "environment")


@dataclass(frozen=True)
class Outcome:
    exit_code: int
    stderr: str
    stdout: str = ""


@dataclass(frozen=True)
class Installer:
    """One installer under test: its guard's module and name, and a call that installs it."""

    name: str
    module: Any
    guard: str
    install: Callable[[Path, str, bool], Outcome]


def _ok(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """A scheduler command runner that accepts everything and lists every timer asked for."""
    listed = "onex-worktree-reconcile.timer\n" if "list-timers" in command else ""
    return subprocess.CompletedProcess(list(command), 0, listed, "")


def _example(node: str) -> dict[str, Any]:
    document = yaml.safe_load(
        (NODES / node / "overlay.example.yaml").read_text(encoding="utf-8")
    )
    assert isinstance(document, dict)
    return document


def _where(tmp_path: Path, vector: str, part: str) -> str:
    """The path of one part: under ``omni_worktrees`` when it is the vector, else stable."""
    base = tmp_path / "fixture"
    return str(base / (WORKTREE if vector == part else "stable-clone") / part)


def _seam(tmp_path: Path, vector: str, dry_run: bool) -> Outcome:
    document = _example("node_seam_deadline_probe_effect")
    host = document["schedule_hosts"][0]
    host["venv"] = _where(tmp_path, vector, "program")
    if vector == "environment":
        host["environment"]["EXAMPLE_LEDGER_APPEND_COMMAND"] = (
            f"{tmp_path}/{WORKTREE}/bin/python -m x"
        )
    root = tmp_path / ("fixture/" + WORKTREE if vector == "workdir" else "registry")
    result = SeamDeadlineScheduler(
        schedule=seam_module.load_schedule(),
        overlay=ModelSeamDeadlineOverlay.model_validate(document),
        home=tmp_path / "home",
        run=_ok,
    ).install(
        host["name"],
        omni_root=root,
        overlay_root=tmp_path / "overlays",
        dry_run=dry_run,
    )
    return Outcome(result.exit_code, result.stderr, result.stdout)


def _copy(tmp_path: Path, vector: str, dry_run: bool) -> Outcome:
    document = _example("node_work_ledger_copy_effect")
    launchd = document["launchd"]
    launchd["interpreter"] = _where(tmp_path, vector, "program")
    launchd["working_directory"] = _where(tmp_path, vector, "workdir")
    if vector == "environment":
        launchd["environment"]["ONEX_SKILL_OVERLAY_ROOTS"] = f"/srv/{WORKTREE}"
    launchd["log_directory"] = str(tmp_path / "home" / "logs")
    return _copy_install(tmp_path, document, dry_run)


def _copy_install(tmp_path: Path, document: dict[str, Any], dry_run: bool) -> Outcome:
    overlay = ModelCopyOverlay.model_validate(document)
    plan = copy_module.load_schedule()
    try:
        if dry_run:
            return Outcome(
                0, "", copy_module.render_plist(overlay, plan).decode("utf-8")
            )
        code = copy_module.install(
            overlay, plan, agents_dir=tmp_path / "home" / "agents", uid=501, run=_ok
        )
    except UnitRootRefusedError as exc:
        return Outcome(1, str(exc))
    return Outcome(code, "")


def _reconcile_overlay(tmp_path: Path, vector: str) -> Path:
    project = Path(_where(tmp_path, vector, "workdir"))
    entry = project / "src/omnimarket/nodes/node_worktree_reconcile_effect/__main__.py"
    entry.parent.mkdir(parents=True)
    entry.write_text("")
    environment = Path(_where(tmp_path, vector, "environment")).with_suffix(".env")
    python = Path(_where(tmp_path, vector, "program"))
    for file in (environment, python):
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("")
    for argv in (
        ["git", "init", "--quiet", str(project)],
        ["git", "-C", str(project), "add", "src"],
        [
            *("git", "-C", str(project)),
            *("-c", "user.name=Test", "-c", "user.email=test@example.invalid"),
            *("commit", "--quiet", "-m", "fixture"),
        ],
    ):
        subprocess.run(argv, check=True, capture_output=True, text=True)
    document = _example_reconcile()
    document.update(
        python=str(python),
        project=str(project),
        state_directory=str(tmp_path / "home" / "state"),
        environment_file=str(environment),
    )
    document["hosts"]["lab-host"]["roots"] = [str(tmp_path / "fixture" / "trees")]
    path = tmp_path / "fixture" / "overlay.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return path


def _example_reconcile() -> dict[str, Any]:
    document = yaml.safe_load(
        (
            NODES / "node_worktree_reconcile_effect/models/overlay.example.yaml"
        ).read_text(encoding="utf-8")
    )
    assert isinstance(document, dict)
    return document


def _reconcile(tmp_path: Path, vector: str, dry_run: bool) -> Outcome:
    path = _reconcile_overlay(tmp_path, vector)
    result = HandlerWorktreeSchedule(
        unit_directory=tmp_path / "home" / "units", runner=_ok
    ).handle(
        ModelWorktreeScheduleRequest(
            mode="render" if dry_run else "install",
            host="lab-host",
            overlay_path=str(path),
        )
    )
    return Outcome(result.exit_code, result.stderr, result.service + result.timer)


INSTALLERS = [
    Installer("seam_deadline", seam_module, "refuse_worktree_roots", _seam),
    Installer("work_ledger_copy", copy_module, "refuse_worktree_plists", _copy),
    Installer(
        "worktree_reconcile", reconcile_module, "refuse_worktree_roots", _reconcile
    ),
]
IDS = [case.name for case in INSTALLERS]


def _written(tmp_path: Path) -> list[Path]:
    home = tmp_path / "home"
    return sorted(path for path in home.rglob("*") if path.is_file())


@pytest.mark.parametrize("vector", VECTORS)
@pytest.mark.parametrize("case", INSTALLERS, ids=IDS)
def test_a_unit_rooted_in_a_worktree_is_refused_and_nothing_is_written(
    case: Installer, vector: str, tmp_path: Path
) -> None:
    result = case.install(tmp_path, vector, False)
    assert result.exit_code == 1, result.stderr
    assert WORKTREE in result.stderr
    assert not _written(tmp_path)


@pytest.mark.parametrize("vector", VECTORS)
@pytest.mark.parametrize("case", INSTALLERS, ids=IDS)
def test_a_dry_run_or_render_refuses_a_worktree_root_too(
    case: Installer, vector: str, tmp_path: Path
) -> None:
    result = case.install(tmp_path, vector, True)
    assert result.exit_code == 1, result.stderr
    assert WORKTREE in result.stderr
    assert "[Timer]" not in result.stdout
    assert "ProgramArguments" not in result.stdout
    assert not _written(tmp_path)


@pytest.mark.parametrize("case", INSTALLERS, ids=IDS)
def test_stable_root_installs_and_writes_no_worktree_text(
    case: Installer, tmp_path: Path
) -> None:
    result = case.install(tmp_path, "stable", False)
    assert result.exit_code == 0, result.stderr
    files = _written(tmp_path)
    assert files, "positive control: the install wrote its unit files"
    for path in files:
        assert "omni_worktrees" not in path.read_bytes().decode("utf-8", "replace")


def test_a_launchd_log_path_under_a_worktree_is_not_refused(tmp_path: Path) -> None:
    """A log is written, not run: only the program, directory and environment are read."""
    document = _example("node_work_ledger_copy_effect")
    document["launchd"]["log_directory"] = f"{tmp_path}/{WORKTREE}/logs"
    result = _copy_install(tmp_path, document, False)
    assert result.exit_code == 0
    plist = next(path for path in _written(tmp_path) if path.suffix == ".plist")
    assert WORKTREE in plist.read_bytes().decode("utf-8")
    assert not any("omni_worktrees" in path for path in plist_paths(plist.read_bytes()))


def test_the_plist_reader_names_each_worktree_path_and_skips_logs() -> None:
    import plistlib

    data = plistlib.dumps(
        {
            "ProgramArguments": [f"/a/{WORKTREE}/bin/python", "-m", "x"],
            "WorkingDirectory": "/srv/registry",
            "EnvironmentVariables": {"PATH": "/usr/bin:/opt/omni_worktrees/t/bin"},
            "StandardOutPath": "/srv/omni_worktrees/log/out.log",
        }
    )
    assert plist_paths(data) == {
        f"/a/{WORKTREE}/bin/python",
        "/srv/registry",
        "/usr/bin",
        "/opt/omni_worktrees/t/bin",
    }
    with pytest.raises(UnitRootRefusedError) as raised:
        refuse_worktree_plists({"x.plist": data})
    assert raised.value.paths == (
        f"/a/{WORKTREE}/bin/python",
        "/opt/omni_worktrees/t/bin",
    )


@pytest.mark.parametrize("case", INSTALLERS, ids=IDS)
def test_with_the_guard_let_through_the_same_install_writes_a_worktree_unit(
    case: Installer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The named injected break: the failing control of the refusal tests above."""
    monkeypatch.setattr(case.module, case.guard, lambda _units: None)
    result = case.install(tmp_path, "program", False)
    assert result.exit_code == 0, result.stderr
    texts = [
        path.read_bytes().decode("utf-8", "replace") for path in _written(tmp_path)
    ]
    assert any("omni_worktrees" in text for text in texts)
