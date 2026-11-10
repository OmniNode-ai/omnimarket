# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The stop drill's hourly timer and its timer entry point (OMN-22943).

The timer: the units come from the contract and the overlay, survive a reboot, refuse a worktree root, and uninstall is
the rollback. The entry point: one run through the real command appender writes the rows and the timer's evidence, an
overlay that names no ledger route refuses with 65, and a ledger command that refuses ends 74.
"""

from __future__ import annotations

import json
import stat
import subprocess
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_stop_drill_effect.handlers.schedule import (
    NODE_NAME,
    StopDrillScheduler,
    load_schedule,
    render_units,
)
from omnimarket.nodes.node_stop_drill_effect.models import ModelStopDrillOverlay
from omnimarket.nodes.node_stop_drill_effect.runtime_stop_drill import (
    EXIT_LEDGER_UNCONFIRMED,
    EXIT_OVERLAY_REFUSED,
    EXIT_WORKING,
    run_once,
    write_evidence,
)
from tests.nodes.node_stop_drill_effect.support import (
    LANE,
    overlay,
)

pytestmark = pytest.mark.unit

NODE_DIR = Path(__file__).parents[3] / "src" / "omnimarket" / "nodes" / NODE_NAME


class _Recorder:
    def __init__(self, failing: str | None = None) -> None:
        self.commands: list[list[str]] = []
        self.failing = failing

    def __call__(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.commands.append(list(command))
        code = 1 if self.failing and self.failing in command else 0
        return subprocess.CompletedProcess(
            list(command), code, "", "boom" if code else ""
        )


def _layout(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / "root"
    python = root / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\n")
    home = tmp_path / "home"
    home.mkdir()
    return root, home, tmp_path / "overlays"


def test_the_example_overlay_loads_through_the_model() -> None:
    document = yaml.safe_load((NODE_DIR / "overlay.example.yaml").read_text())
    assert ModelStopDrillOverlay.model_validate(document).emission.piece_number == 7


def test_the_units_run_the_run_verb_hourly_and_survive_a_reboot(tmp_path: Path) -> None:
    root, home, overlays = _layout(tmp_path)
    service, timer = render_units(
        load_schedule(),
        overlay(),
        omni_root=root,
        venv=root / "venv",
        home=home,
        overlay_root=overlays,
    )
    assert (
        "-m omnimarket.nodes.node_stop_drill_effect run --trigger schedule" in service
    )
    assert f'Environment="OMNI_HOME={root}"' in service
    assert f'Environment="TEST_HOME={root}"' in service  # $OMNI_HOME filled in
    assert "OnCalendar=*-*-* *:20:00" in timer
    assert "Persistent=true" in timer
    assert "Unit=onex-stop-drill-hourly.service" in timer


def test_install_writes_two_units_and_enables_the_timer_and_uninstall_is_the_rollback(
    tmp_path: Path,
) -> None:
    root, home, overlays = _layout(tmp_path)
    recorder = _Recorder()
    scheduler = StopDrillScheduler(
        schedule=load_schedule(), overlay=overlay(), home=home, run=recorder
    )
    done = scheduler.install("h053", omni_root=root, overlay_root=overlays)
    assert done.exit_code == 0, done.stderr
    unit_dir = home / ".config/systemd/user"
    assert (unit_dir / "onex-stop-drill-hourly.service").is_file()
    assert (unit_dir / "onex-stop-drill-hourly.timer").is_file()
    assert [
        "systemctl",
        "--user",
        "enable",
        "--now",
        "onex-stop-drill-hourly.timer",
    ] in recorder.commands
    back = scheduler.uninstall("h053")
    assert back.exit_code == 0
    assert not list(unit_dir.iterdir())


def test_install_refuses_a_host_the_overlay_does_not_declare(tmp_path: Path) -> None:
    root, home, overlays = _layout(tmp_path)
    scheduler = StopDrillScheduler(
        schedule=load_schedule(), overlay=overlay(), home=home, run=_Recorder()
    )
    refused = scheduler.install("h999", omni_root=root, overlay_root=overlays)
    assert refused.exit_code == 1
    assert (
        "not in the node_stop_drill_effect overlay's schedule_hosts" in refused.stderr
    )


def test_install_refuses_a_unit_that_would_run_from_a_worktree(tmp_path: Path) -> None:
    root = tmp_path / "omni_worktrees" / "OMN-1" / "omnimarket"
    python = root / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\n")
    home = tmp_path / "home"
    home.mkdir()
    scheduler = StopDrillScheduler(
        schedule=load_schedule(), overlay=overlay(), home=home, run=_Recorder()
    )
    refused = scheduler.install("h053", omni_root=root, overlay_root=tmp_path / "o")
    assert refused.exit_code == 1
    assert not (home / ".config").exists()


def test_check_fails_when_no_schedule_host_is_declared() -> None:
    scheduler = StopDrillScheduler(
        schedule=load_schedule(),
        overlay=overlay(schedule_hosts=[]),
        home=Path("/nonexistent"),
        run=_Recorder(),
    )
    assert scheduler.check().exit_code == 1


def _append_command(tmp_path: Path, rows_file: Path, exit_code: int = 0) -> str:
    script = tmp_path / "append.py"
    script.write_text(
        "import json, sys\n"
        f"open({str(rows_file)!r}, 'a').write(sys.stdin.read())\n"
        "print(json.dumps({'status': 'accepted'}))\n"
        f"sys.exit({exit_code})\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return f"{sys.executable} {script}"


def _environ(tmp_path: Path, command: str) -> dict[str, str]:
    root = tmp_path / "overlays" / NODE_NAME
    root.mkdir(parents=True)
    document = overlay().model_dump(mode="json")
    (root / "overlay.yaml").write_text(yaml.safe_dump(document))
    return {
        "ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "overlays"),
        "TEST_HOME": str(tmp_path / "home"),
        "TEST_HOST": "h053",
        "TEST_APPEND_COMMAND": command,
    }


def test_run_once_appends_the_two_rows_through_the_command_and_writes_the_journal(
    tmp_path: Path,
) -> None:
    rows_file = tmp_path / "rows.txt"
    env = _environ(tmp_path, _append_command(tmp_path, rows_file))
    out: list[str] = []
    errors: list[str] = []
    code, run_id, verdict = run_once(
        "manual-1",
        trigger="manual",
        emit=out.append,
        emit_error=errors.append,
        environ=env,
    )
    assert (code, verdict, errors) == (EXIT_WORKING, "WORKING", [])
    rows = [line for line in rows_file.read_text().splitlines() if line.strip()]
    assert len(rows) == 2
    assert f"lane={LANE}" in rows[0]
    assert "verdict=WORKING" in rows[0]
    assert "drill=no" in rows[0]
    assert "verdict=NOT_WORKING" in rows[1]
    assert "drill=yes" in rows[1]
    journal = tmp_path / "home/.onex_state/floor-simple/emissions.jsonl"
    phases = [json.loads(line)["phase"] for line in journal.read_text().splitlines()]
    assert phases == ["start", "success", "start", "failure"]
    assert json.loads(out[0])["verdict"] == "WORKING"
    write_evidence(tmp_path / "state", run_id, verdict, code, datetime.now(UTC))
    assert json.loads((tmp_path / "state/last-run.json").read_text())["exit_code"] == 0


def test_run_once_exits_74_when_the_ledger_command_does_not_return_a_receipt(
    tmp_path: Path,
) -> None:
    command = f"{sys.executable} -c pass"
    env = _environ(tmp_path, command)
    errors: list[str] = []
    code, _, verdict = run_once(
        "manual-2",
        trigger="manual",
        emit=lambda _t: None,
        emit_error=errors.append,
        environ=env,
    )
    assert code == EXIT_LEDGER_UNCONFIRMED
    assert verdict == ""
    assert errors
    assert errors[0].startswith("ledger:")


def test_run_once_exits_65_when_the_ledger_route_variable_is_unset(
    tmp_path: Path,
) -> None:
    env = _environ(tmp_path, "unused")
    del env["TEST_APPEND_COMMAND"]
    errors: list[str] = []
    code, _, _ = run_once(
        "manual-3",
        trigger="manual",
        emit=lambda _t: None,
        emit_error=errors.append,
        environ=env,
    )
    assert code == EXIT_OVERLAY_REFUSED
    assert errors[0].startswith("overlay refused:")


def test_run_once_exits_65_when_no_overlay_is_found(tmp_path: Path) -> None:
    errors: list[str] = []
    code, _, _ = run_once(
        None,
        trigger="manual",
        emit=lambda _t: None,
        emit_error=errors.append,
        environ={"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "none")},
    )
    assert code == EXIT_OVERLAY_REFUSED
