# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_host_installer_registry_units: a rollback puts the previous install of a unit back (OMN-24108).

An install that replaces a unit keeps the files it replaces, one level deep; a rollback copies them back and
consumes them. Launchd registry units, systemd units and copied-tree units each have a case. The failing control is
the refusal: a rollback with nothing kept changes nothing and says so.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.nodes.node_host_installer_registry_units.handlers import (
    handler_registry_units,
)
from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    HandlerRegistryUnits,
    load_units,
)
from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_unit_rollback import (
    NoPreviousInstallError,
    keep_previous,
    read_previous,
    restore_previous,
)
from omnimarket.nodes.node_host_installer_registry_units.models.model_registry_units import (
    ModelRegistryUnitsRequest,
)

HANDLER = "omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units"
UNIT = "factory-floor-publish"
LABEL = "ai.omninode.factory-floor-publish"
BREW_PYTHON = "/opt/homebrew/bin/python3.13"
LINUX_PYTHON = "/usr/bin/python3.12"
SYSTEMD_UNIT = "overlay-store-refresh-systemd"
SYSTEMD_LABEL = "onex-overlay-store-refresh"
SYSTEMD_ENTRY = (
    "src/omnimarket/nodes/node_overlay_store_read_effect/runtime_overlay_store_tick.py"
)
FLOOR_ENTRY = "src/omnimarket/nodes/node_factory_floor_publish_effect/__main__.py"


def _git(argv: list[str]) -> str:
    done = subprocess.run(
        argv,
        env=scrub_git_location_env(os.environ),
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


def make_checkout(path: Path, entry: str, text: str = "print('tick')\n") -> str:
    path.mkdir(parents=True)
    _git(["git", "-C", str(path), "init", "-q", "-b", "branch"])
    (path / entry).parent.mkdir(parents=True)
    (path / entry).write_text(text, encoding="utf-8")
    _git(["git", "-C", str(path), "add", "-A"])
    _git(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.com",
            "commit",
            "-q",
            "-m",
            "x",
        ]
    )
    return _git(["git", "-C", str(path), "rev-parse", "HEAD"])


class Recorder:
    """Real git and id; every launchctl, systemctl and uv call is recorded and answered with success."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        if argv[0] in ("git", "id"):
            return subprocess.run(
                argv,
                env=scrub_git_location_env(os.environ),
                capture_output=True,
                text=True,
                check=False,
            )
        return subprocess.CompletedProcess(argv, 0, "", "")

    def verbs(self, program: str) -> list[str]:
        return [
            c[1] if program == "launchctl" else c[2]
            for c in self.calls
            if c[0] == program
        ]


@pytest.fixture(autouse=True)
def hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        f"{HANDLER}.interpreter_present",
        lambda path: path in (BREW_PYTHON, LINUX_PYTHON),
    )
    monkeypatch.setattr(
        "shutil.which", lambda name: "/usr/bin/uv" if name == "uv" else None
    )


def request(
    tmp_path: Path, branch: Path, revision: str, **kw: Any
) -> ModelRegistryUnitsRequest:
    fields: dict[str, Any] = {
        "unit": UNIT,
        "mode": "install",
        "home": str(tmp_path / "home"),
        "registry_root": str(tmp_path / "omni"),
        "workspace_config_root": str(tmp_path / "ws-a"),
        "plans_path": str(tmp_path / "plans"),
        "marketplace_root": str(tmp_path / "market"),
        "runtime_ssh_host": "lab-runtime",
        "ledger_host_name": "lab-ledger-host",
        "project_path": str(branch),
        "expected_revision": revision,
    }
    fields.update(kw)
    return ModelRegistryUnitsRequest(**fields)


@pytest.fixture
def floor(tmp_path: Path) -> tuple[Path, str]:
    branch = tmp_path / "branch"
    return branch, make_checkout(branch, FLOOR_ENTRY)


def plist_path(tmp_path: Path) -> Path:
    return tmp_path / "home" / "Library/LaunchAgents" / f"{LABEL}.plist"


def kept_path(tmp_path: Path, unit: str = UNIT) -> Path:
    return tmp_path / "omni" / ".onex_state" / unit / f"{unit}.previous"


def test_a_rollback_puts_the_previous_plist_and_record_back_and_uses_the_kept_copy_up(
    tmp_path: Path, floor: tuple[Path, str]
) -> None:
    branch, revision = floor
    run = Recorder()
    handler = HandlerRegistryUnits(run)
    first = handler.handle(request(tmp_path, branch, revision))
    assert first.status == "success", first.stderr
    first_plist = plist_path(tmp_path).read_bytes()
    first_record = Path(first.record).read_bytes()
    second = handler.handle(
        request(
            tmp_path, branch, revision, workspace_config_root=str(tmp_path / "ws-b")
        )
    )
    assert second.status == "success", second.stderr
    assert plist_path(tmp_path).read_bytes() != first_plist
    run.calls.clear()
    back = handler.handle(request(tmp_path, branch, revision, mode="rollback"))
    assert back.status == "success", back.stderr
    assert plist_path(tmp_path).read_bytes() == first_plist
    assert Path(first.record).read_bytes() == first_record
    assert run.verbs("launchctl") == ["bootout", "bootstrap"]
    assert not kept_path(tmp_path).exists()


def test_a_second_rollback_straight_after_refuses_and_changes_nothing(
    tmp_path: Path, floor: tuple[Path, str]
) -> None:
    branch, revision = floor
    handler = HandlerRegistryUnits(Recorder())
    handler.handle(request(tmp_path, branch, revision))
    handler.handle(
        request(
            tmp_path, branch, revision, workspace_config_root=str(tmp_path / "ws-b")
        )
    )
    assert (
        handler.handle(request(tmp_path, branch, revision, mode="rollback")).exit_code
        == 0
    )
    before = plist_path(tmp_path).read_bytes()
    again = handler.handle(request(tmp_path, branch, revision, mode="rollback"))
    assert again.exit_code == 1
    assert "nothing to roll back" in again.stderr
    assert plist_path(tmp_path).read_bytes() == before


def test_a_rollback_with_no_install_kept_is_refused_without_touching_launchd(
    tmp_path: Path, floor: tuple[Path, str]
) -> None:
    branch, revision = floor
    run = Recorder()
    result = HandlerRegistryUnits(run).handle(
        request(tmp_path, branch, revision, mode="rollback")
    )
    assert result.exit_code == 1
    assert "nothing to roll back" in result.stderr
    assert run.verbs("launchctl") == []


def test_a_repeated_identical_install_keeps_the_earlier_previous_copy(
    tmp_path: Path, floor: tuple[Path, str]
) -> None:
    branch, revision = floor
    handler = HandlerRegistryUnits(Recorder())
    handler.handle(request(tmp_path, branch, revision))
    first_plist = plist_path(tmp_path).read_bytes()
    second = request(
        tmp_path, branch, revision, workspace_config_root=str(tmp_path / "ws-b")
    )
    handler.handle(second)
    handler.handle(second)
    assert (
        handler.handle(request(tmp_path, branch, revision, mode="rollback")).exit_code
        == 0
    )
    assert plist_path(tmp_path).read_bytes() == first_plist


def test_a_rollback_of_the_first_and_only_install_removes_the_unit_files(
    tmp_path: Path, floor: tuple[Path, str]
) -> None:
    branch, revision = floor
    handler = HandlerRegistryUnits(Recorder())
    installed = handler.handle(request(tmp_path, branch, revision))
    assert plist_path(tmp_path).exists()
    back = handler.handle(request(tmp_path, branch, revision, mode="rollback"))
    assert back.exit_code == 0, back.stderr
    assert not plist_path(tmp_path).exists()
    assert not Path(installed.record).exists()
    assert "was the first" in back.stdout


def test_a_dry_run_a_status_read_and_a_kickstart_keep_nothing(
    tmp_path: Path, floor: tuple[Path, str]
) -> None:
    branch, revision = floor
    handler = HandlerRegistryUnits(Recorder())
    for mode in ("dry-run", "status", "kickstart"):
        handler.handle(request(tmp_path, branch, revision, mode=mode))
    assert not kept_path(tmp_path).exists()


def test_a_systemd_unit_rolls_back_its_service_and_timer(tmp_path: Path) -> None:
    branch = tmp_path / "branch"
    revision = make_checkout(branch, SYSTEMD_ENTRY)
    run = Recorder()
    handler = HandlerRegistryUnits(run)

    def systemd(**kw: Any) -> ModelRegistryUnitsRequest:
        fields: dict[str, Any] = {
            "unit": SYSTEMD_UNIT,
            "mode": "install",
            "home": str(tmp_path / "home"),
            "registry_root": str(tmp_path / "omni"),
            "project_path": str(branch),
            "expected_revision": revision,
            "path_overrides": {
                "ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "overlays" / "overlays"),
                "LAB_WORK_HOST_NAME": "h051",
            },
        }
        fields.update(kw)
        return ModelRegistryUnitsRequest(**fields)

    unit_dir = tmp_path / "home/.config/systemd/user"
    service, timer = (
        unit_dir / f"{SYSTEMD_LABEL}.{ext}" for ext in ("service", "timer")
    )
    assert handler.handle(systemd()).status == "success"
    first = (service.read_bytes(), timer.read_bytes())
    again = handler.handle(
        systemd(
            path_overrides={
                "ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "other" / "overlays"),
                "LAB_WORK_HOST_NAME": "h051",
            }
        )
    )
    assert again.status == "success", again.stderr
    assert (service.read_bytes(), timer.read_bytes()) != first
    run.calls.clear()
    back = handler.handle(systemd(mode="rollback"))
    assert back.status == "success", back.stderr
    assert (service.read_bytes(), timer.read_bytes()) == first
    assert "daemon-reload" in run.verbs("systemctl")
    assert "restart" in run.verbs("systemctl")
    assert not kept_path(tmp_path, SYSTEMD_UNIT).exists()


def copied_source(root: Path, marker: str) -> Path:
    source = root / "plugin"
    files = {
        "skills/merge-drain/scripts/floor_alarm.py": (
            "import argparse\n"
            "p = argparse.ArgumentParser()\n"
            "p.add_argument('--state-dir', required=True)\n"
            "p.add_argument('--ledger', required=True)\n"
            "p.parse_args()\n"
            f"# {marker}\n"
        ),
        "skills/merge-drain/scripts/landing_scorecard.py": "# scorecard\n",
        "skills/merge-drain/scripts/pr_state_local.py": "# watcher reader\n",
        "skills/merge-drain/landing_floors.json": "{}\n",
        "skills/queue-status/scripts/queue_status.py": "# queue reader\n",
        "src/alarm_support/__init__.py": "",
    }
    for name, text in files.items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return source


def test_a_copied_tree_unit_rolls_back_its_payload_and_plist(tmp_path: Path) -> None:
    units = load_units()
    units["floor-alarm"]["interpreter_candidates"] = [sys.executable]
    run = Recorder()
    handler = HandlerRegistryUnits(run=run, units=units)

    def copied(source: Path, mode: str = "install") -> ModelRegistryUnitsRequest:
        return ModelRegistryUnitsRequest(
            unit="floor-alarm",
            mode=mode,
            home=str(tmp_path / "home"),
            registry_root=str(tmp_path / "registry"),
            source_root=str(source),
        )

    installed = tmp_path / "home/.omninode/floor-alarm"
    script = installed / "skills/merge-drain/scripts/floor_alarm.py"
    plist = tmp_path / "home/Library/LaunchAgents/ai.omninode.floor-alarm.plist"
    # the proof runs the staged script with the real interpreter; the other calls are recorded
    real_run = run.__call__

    def run_with_interpreter(argv: list[str]) -> subprocess.CompletedProcess[str]:
        if argv[0] == sys.executable:
            return subprocess.run(
                argv, capture_output=True, text=True, check=False, timeout=30
            )
        if argv[:2] == ["id", "-u"]:
            return subprocess.CompletedProcess(argv, 0, "501\n", "")
        if argv[:2] == ["id", "-un"]:
            return subprocess.CompletedProcess(argv, 0, "fixture-user\n", "")
        return real_run(argv)

    handler = HandlerRegistryUnits(run=run_with_interpreter, units=units)
    assert handler.handle(copied(copied_source(tmp_path / "a", "first"))).exit_code == 0
    first = (script.read_bytes(), plist.read_bytes())
    assert (
        handler.handle(copied(copied_source(tmp_path / "b", "second"))).exit_code == 0
    )
    assert script.read_bytes() != first[0]
    back = handler.handle(copied(copied_source(tmp_path / "b", "second"), "rollback"))
    assert back.exit_code == 0, back.stderr
    assert (script.read_bytes(), plist.read_bytes()) == first


def test_the_kept_copy_round_trips_a_file_a_directory_and_an_absent_target(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    file_target = tmp_path / "unit.plist"
    file_target.write_text("one", encoding="utf-8")
    tree = tmp_path / "payload"
    (tree / "sub").mkdir(parents=True)
    (tree / "sub/a.txt").write_text("a", encoding="utf-8")
    absent = tmp_path / "record.json"
    keep_previous(state, "u", {"plist": file_target, "payload": tree, "record": absent})
    file_target.write_text("two", encoding="utf-8")
    (tree / "sub/a.txt").write_text("changed", encoding="utf-8")
    absent.write_text("new", encoding="utf-8")
    kept, manifest = read_previous(state, "u")
    restore_previous(kept, manifest)
    assert file_target.read_text(encoding="utf-8") == "one"
    assert (tree / "sub/a.txt").read_text(encoding="utf-8") == "a"
    assert not absent.exists()
    assert not kept.exists()


def test_reading_a_previous_install_that_was_never_kept_is_refused(
    tmp_path: Path,
) -> None:
    with pytest.raises(NoPreviousInstallError, match="nothing to roll back"):
        read_previous(tmp_path, "u")


def test_a_rollback_takes_the_lock_so_it_never_runs_beside_an_install_of_the_unit() -> (
    None
):
    assert "rollback" in handler_registry_units.LOCKED_MODES
    assert "install" in handler_registry_units.LOCKED_MODES
    assert "dry-run" not in handler_registry_units.LOCKED_MODES
