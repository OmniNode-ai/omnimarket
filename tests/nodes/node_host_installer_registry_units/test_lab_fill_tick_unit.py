# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_host_installer_registry_units: the lab fill is a declared 1200-second systemd unit on a lab host.

The fill ran as a launchd job on the operator Mac, which cannot read the lab's state, and refused every pass. The installer now
declares it as a systemd timer unit that runs omnimarket's runtime_lab_fill_tick on a pool host, and the Mac entry is gone.
"""

from __future__ import annotations

import importlib
import io
import json
from pathlib import Path

import pytest

from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    HandlerRegistryUnits,
    load_units,
)
from tests.nodes.node_host_installer_registry_units import (
    test_registry_units_systemd as base,
)

pytestmark = pytest.mark.unit

UNIT = "lab-fill-tick-systemd"
LABEL = "onex-filler-tick"
MODULE = "omnimarket.nodes.node_lab_fill_effect.runtime_lab_fill_tick"
ENTRY = "src/omnimarket/nodes/node_lab_fill_effect/runtime_lab_fill_tick.py"
INTERVAL_SECONDS = 1200
RUN_LIMIT_SECONDS = 1020


@pytest.fixture(autouse=True)
def linux_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        f"{base.HANDLER}.interpreter_present", lambda path: path == base.LINUX_PYTHON
    )
    monkeypatch.setattr(
        "shutil.which", lambda name: "/usr/bin/uv" if name == "uv" else None
    )


def _checkout(tmp_path: Path) -> tuple[Path, str]:
    path = tmp_path / "branch"
    base.make_checkout(path)
    (path / ENTRY).parent.mkdir(parents=True, exist_ok=True)
    (path / ENTRY).write_text("print('tick')\n", encoding="utf-8")
    base._git(["git", "-C", str(path), "add", "-A"], check=True)
    base._git(
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
            "y",
        ],
        check=True,
    )
    revision = base._git(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return path, revision


def test_the_unit_is_declared_every_1200_seconds_on_systemd_and_runs_the_tick_entry() -> (
    None
):
    unit = load_units()[UNIT]
    assert unit["platform"] == "systemd"
    assert unit["label"] == LABEL
    assert unit["schedule"]["start_interval"] == INTERVAL_SECONDS
    assert unit["schedule"]["run_timeout_s"] == RUN_LIMIT_SECONDS
    assert unit["schedule"]["run_timeout_s"] < unit["schedule"]["start_interval"]
    assert unit["schedule"]["run_at_load"] is True
    assert unit["module"] == MODULE
    assert unit["entry_file"] == ENTRY
    assert unit["probe_argv"] == ["--probe"]
    assert unit["environment"]["ONEX_SKILL_OVERLAY_ROOTS"] == "{overlays_root}"
    assert unit["environment"]["OMNIBASE_PATH"] == "{registry_root}"
    assert unit["environment"]["ONEX_LEDGER_BUS_READ_COMMAND"].startswith(
        "{registry_root}/"
    )


def test_the_mac_launchd_entry_is_gone() -> None:
    """The move: no launchd unit carries the fill, so the operator Mac's job has no declaration to install again."""
    units = load_units()
    assert "lab-fill-tick" not in units
    assert not [
        name
        for name, unit in units.items()
        if unit.get("label") == "ai.omninode.lab-fill"
        or (unit.get("module") == MODULE and unit.get("platform") != "systemd")
    ]


def test_the_declared_entry_file_and_module_exist_and_the_probe_answers() -> None:
    unit = load_units()[UNIT]
    module = importlib.import_module(unit["module"])
    assert (Path(__file__).parents[3] / unit["entry_file"]).is_file()
    out = io.StringIO()
    assert module.main(unit["probe_argv"], environ={}, stdout=out) == 0
    assert json.loads(out.getvalue()) == {"status": "probe_ok"}


def test_install_renders_a_1200_second_timer_running_the_module_from_the_branch_checkout(
    tmp_path: Path,
) -> None:
    path, revision = _checkout(tmp_path)
    run = base.Recorder()
    result = HandlerRegistryUnits(run).handle(
        base.request(
            tmp_path,
            unit=UNIT,
            mode="install",
            project_path=str(path),
            expected_revision=revision,
            workspace_config_root=str(tmp_path / "config"),
            path_overrides={},
        )
    )
    assert result.status == "success", result.stderr
    unit_dir = tmp_path / "home" / ".config/systemd/user"
    service = (unit_dir / f"{LABEL}.service").read_text(encoding="utf-8")
    timer = (unit_dir / f"{LABEL}.timer").read_text(encoding="utf-8")
    assert f"OnUnitActiveSec={INTERVAL_SECONDS}" in timer
    assert f'"-m" "{MODULE}"' in service
    assert f"TimeoutStartSec={RUN_LIMIT_SECONDS}" in service
    assert f'"OMNIBASE_PATH={tmp_path}/omni"' in service
    assert f'"ONEX_SKILL_OVERLAY_ROOTS={tmp_path}/omni/overlays/overlays"' in service
    assert f'"PATH={tmp_path}/omni/omnibase_infra/scripts' in service
    assert "lab-fill.js" not in service
    assert run.systemctl() == [
        ["daemon-reload"],
        ["enable", f"{LABEL}.timer"],
        ["restart", f"{LABEL}.timer"],
    ]


def test_a_checkout_without_the_tick_entry_file_is_refused(tmp_path: Path) -> None:
    """Error chain: the installer refuses a branch that does not carry the lab-fill entry file."""
    path = tmp_path / "branch"
    base.make_checkout(path)
    result = HandlerRegistryUnits(base.Recorder()).handle(
        base.request(
            tmp_path,
            unit=UNIT,
            mode="install",
            project_path=str(path),
            path_overrides={},
        )
    )
    assert result.status == "failure"
    assert not (tmp_path / "home/.config/systemd/user" / f"{LABEL}.timer").exists()
