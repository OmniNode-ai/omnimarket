# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_host_installer_registry_units: a unit declaring platform systemd installs a user service and timer (OMN-22878).

The overlay store reader keeps the host-list projection on a Linux pool host; until this unit existed it had no
declared installer there, only a hand-started transient unit.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    HandlerRegistryUnits,
    load_units,
)
from omnimarket.nodes.node_host_installer_registry_units.models.model_registry_units import (
    ModelRegistryUnitsRequest,
)

ROOT = Path(__file__).resolve().parents[3]
UNIT = "overlay-store-refresh-systemd"
LABEL = "onex-overlay-store-refresh"
ENTRY = (
    "src/omnimarket/nodes/node_overlay_store_read_effect/runtime_overlay_store_tick.py"
)
LINUX_PYTHON = "/usr/bin/python3.12"
HANDLER = "omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units"


def _git(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, env=scrub_git_location_env(os.environ), **kw)


class Recorder:
    """Real git and id; every systemctl and uv call is recorded and answered by ``answer``."""

    def __init__(self, fail: str = "") -> None:
        self.calls: list[list[str]] = []
        self.fail = fail
        self.active = True

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        if argv[0] in ("git", "id"):
            return _git(argv, capture_output=True, text=True, check=False)
        if argv[0] == "systemctl" and self.fail and self.fail in argv:
            return subprocess.CompletedProcess(argv, 1, "", f"{self.fail} refused")
        if argv[0] == "systemctl" and "is-active" in argv:
            code = 0 if self.active else 3
            return subprocess.CompletedProcess(
                argv, code, "active\n" if self.active else "inactive\n", ""
            )
        return subprocess.CompletedProcess(argv, 0, "", "")

    def systemctl(self) -> list[list[str]]:
        return [c[2:] for c in self.calls if c[0] == "systemctl"]


def make_checkout(path: Path) -> str:
    path.mkdir(parents=True)
    _git(["git", "-C", str(path), "init", "-q", "-b", "branch"], check=True)
    (path / ENTRY).parent.mkdir(parents=True)
    (path / ENTRY).write_text("print('tick')\n", encoding="utf-8")
    _git(["git", "-C", str(path), "add", "-A"], check=True)
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
        ],
        check=True,
    )
    return _git(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def request(tmp_path: Path, **kw: Any) -> ModelRegistryUnitsRequest:
    fields: dict[str, Any] = {
        "unit": UNIT,
        "home": str(tmp_path / "home"),
        "registry_root": str(tmp_path / "omni"),
        "path_overrides": {
            "ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "overlays" / "overlays"),
            "LAB_WORK_HOST_NAME": "h051",
        },
    }
    fields.update(kw)
    return ModelRegistryUnitsRequest(**fields)


@pytest.fixture(autouse=True)
def linux_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        f"{HANDLER}.interpreter_present", lambda path: path == LINUX_PYTHON
    )
    monkeypatch.setattr(
        "shutil.which", lambda name: "/usr/bin/uv" if name == "uv" else None
    )


@pytest.fixture
def branch(tmp_path: Path) -> tuple[Path, str]:
    path = tmp_path / "branch"
    return path, make_checkout(path)


def unit_paths(tmp_path: Path) -> tuple[Path, Path]:
    unit_dir = tmp_path / "home" / ".config/systemd/user"
    return unit_dir / f"{LABEL}.service", unit_dir / f"{LABEL}.timer"


def test_dry_run_renders_a_service_and_timer_that_run_the_branch_checkout(
    tmp_path: Path, branch: tuple[Path, str]
) -> None:
    path, revision = branch
    result = HandlerRegistryUnits(Recorder()).handle(
        request(tmp_path, project_path=str(path), expected_revision=revision)
    )
    assert result.status == "success", result.stderr
    assert result.revision == revision
    assert not result.project_is_canonical
    exec_line = next(
        line for line in result.rendered.splitlines() if line.startswith("ExecStart=")
    )
    assert f'"--project" "{path}"' in exec_line
    assert f'"--python" "{LINUX_PYTHON}"' in exec_line
    assert '"--no-sync"' in exec_line
    assert (
        '"-m" "omnimarket.nodes.node_overlay_store_read_effect.runtime_overlay_store_tick"'
        in exec_line
    )
    assert '"LAB_WORK_HOST_NAME=h051"' in result.rendered
    assert f'"ONEX_SKILL_OVERLAY_ROOTS={tmp_path}/overlays/overlays"' in result.rendered
    assert "TimeoutStartSec=240" in result.rendered
    assert "OnActiveSec=1s" in result.rendered
    assert "OnUnitActiveSec=300" in result.rendered
    assert f"Unit={LABEL}.service" in result.rendered
    assert "__" not in result.rendered.replace("__init__", "")
    assert not unit_paths(tmp_path)[0].exists()


def test_install_writes_both_units_enables_the_timer_and_records_the_revision(
    tmp_path: Path, branch: tuple[Path, str]
) -> None:
    path, revision = branch
    run = Recorder()
    result = HandlerRegistryUnits(run).handle(
        request(
            tmp_path, mode="install", project_path=str(path), expected_revision=revision
        )
    )
    assert result.status == "success", result.stderr
    service, timer = unit_paths(tmp_path)
    assert f'"--project" "{path}"' in service.read_text(encoding="utf-8")
    assert "OnUnitActiveSec=300" in timer.read_text(encoding="utf-8")
    assert run.systemctl() == [
        ["daemon-reload"],
        ["enable", f"{LABEL}.timer"],
        ["restart", f"{LABEL}.timer"],
    ]
    assert not [c for c in run.calls if c[0] == "launchctl"]
    proof = next(c for c in run.calls if c[0] == "env" and "/usr/bin/uv" in c)
    assert str(path) in proof
    assert "--no-sync" not in proof
    record = json.loads(Path(result.record).read_text(encoding="utf-8"))
    assert record["revision"] == revision
    assert record["platform"] == "systemd"
    assert record["project"] == str(path)


def test_a_refused_enable_puts_the_previous_unit_back(
    tmp_path: Path, branch: tuple[Path, str]
) -> None:
    path, revision = branch
    service, timer = unit_paths(tmp_path)
    service.parent.mkdir(parents=True)
    service.write_text("old service\n", encoding="utf-8")
    timer.write_text("old timer\n", encoding="utf-8")
    run = Recorder(fail="enable")
    result = HandlerRegistryUnits(run).handle(
        request(
            tmp_path, mode="install", project_path=str(path), expected_revision=revision
        )
    )
    assert result.status == "failure"
    assert "previous unit restored" in result.stderr
    assert service.read_text(encoding="utf-8") == "old service\n"
    assert timer.read_text(encoding="utf-8") == "old timer\n"
    assert not (
        tmp_path
        / "omni"
        / ".onex_state/overlay-store-refresh"
        / f"{UNIT}.installed.json"
    ).exists()


def test_a_refused_first_install_leaves_no_unit_behind(
    tmp_path: Path, branch: tuple[Path, str]
) -> None:
    path, revision = branch
    result = HandlerRegistryUnits(Recorder(fail="daemon-reload")).handle(
        request(
            tmp_path, mode="install", project_path=str(path), expected_revision=revision
        )
    )
    assert result.status == "failure"
    service, timer = unit_paths(tmp_path)
    assert not service.exists()
    assert not timer.exists()


def test_status_reads_the_timer_and_the_heartbeat(tmp_path: Path) -> None:
    evidence = (
        tmp_path / "home" / ".local/state/omni/overlay-store-refresh/last-run.json"
    )
    run = Recorder()
    run.active = False
    stopped = HandlerRegistryUnits(run).handle(request(tmp_path, mode="status"))
    assert stopped.status == "failure"
    assert f"{LABEL} NOT RUNNING: timer is inactive" in stopped.stdout
    evidence.parent.mkdir(parents=True)
    evidence.write_text("{}\n", encoding="utf-8")
    run.active = True
    fresh = HandlerRegistryUnits(run).handle(request(tmp_path, mode="status"))
    assert fresh.status == "success", fresh.stdout
    old = time.time() - 3 * 300 - 60
    os.utime(evidence, (old, old))
    stale = HandlerRegistryUnits(run).handle(request(tmp_path, mode="status"))
    assert stale.status == "failure"
    assert "is older than 900 s" in stale.stdout


def test_a_value_systemd_cannot_carry_is_refused(
    tmp_path: Path, branch: tuple[Path, str]
) -> None:
    path, revision = branch
    result = HandlerRegistryUnits(Recorder()).handle(
        request(
            tmp_path,
            project_path=str(path),
            expected_revision=revision,
            path_overrides={
                "ONEX_SKILL_OVERLAY_ROOTS": 'a"b',
                "LAB_WORK_HOST_NAME": "h051",
            },
        )
    )
    assert result.status == "failure"
    assert "systemd unit cannot carry" in result.stderr


def test_the_contract_declares_the_reader_on_systemd_and_its_module_exists() -> None:
    unit = load_units()[UNIT]
    assert unit["platform"] == "systemd"
    assert unit["module"] == load_units()["overlay-store-refresh"]["module"]
    assert (ROOT / str(unit["entry_file"])).is_file()
    node_dir = ROOT / "src/omnimarket/nodes/node_host_installer_registry_units"
    assert (node_dir / str(unit["template"])).is_file()
    assert (node_dir / str(unit["timer_template"])).is_file()
