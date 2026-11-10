# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The daily timer of the defect scan (OMN-23331): the units follow the contract's schedule block, install enables the timer, uninstall
is its rollback, and a variable value a unit line cannot carry refuses the render."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from omnimarket.nodes.node_delegation_events_defect_scan_effect.handlers import (
    schedule as sched,
)
from tests.nodes.node_delegation_events_defect_scan_effect.defect_scan_run_support import (
    effect_overlay,
)

pytestmark = pytest.mark.unit

ROOT = Path("/srv/omni")
HOME = Path("/home/tester")
PYTHON = Path("/srv/omni/venv/bin/python")


class Recorder:
    def __init__(self, fail_on: str | None = None) -> None:
        self.commands: list[list[str]] = []
        self.fail_on = fail_on

    def __call__(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.commands.append(list(command))
        code = 5 if self.fail_on and self.fail_on in command else 0
        return subprocess.CompletedProcess(list(command), code, "", "")


def test_delegation_events_defect_scan_the_timer_is_daily_and_follows_the_contract() -> (
    None
):
    schedule = sched.load_schedule()
    service, timer = sched.render_systemd(
        schedule, omni_root=ROOT, home=HOME, python=PYTHON, environment={}
    )
    assert schedule.calendar == "*-*-* 06:15:00 UTC"
    assert "OnCalendar=*-*-* 06:15:00 UTC\n" in timer
    assert "Persistent=true\n" in timer
    assert f"Unit={schedule.unit_name}.service\n" in timer
    assert (
        "ExecStart=/srv/omni/venv/bin/python -m "
        "omnimarket.nodes.node_delegation_events_defect_scan_effect scan\n"
    ) in service
    assert "Type=oneshot\n" in service


def test_delegation_events_defect_scan_the_unit_carries_the_overlays_named_variables_when_set() -> (
    None
):
    overlay = effect_overlay()
    environment = {
        "TEST_RUNTIME_SSH_HOST": "user@host.example",
        "TEST_LEDGER_APPEND_COMMAND": "onex-ledger-append --bus",
        "TEST_LEDGER_HOST_NAME": "h-test",
        "ONEX_SKILL_OVERLAY_ROOTS": "/srv/overlays",
        "UNRELATED_SECRET": "must-not-appear",
    }
    service, _ = sched.render_systemd(
        sched.load_schedule(),
        omni_root=ROOT,
        home=HOME,
        python=PYTHON,
        environment=environment,
        extra_environment_names=overlay.carried_environment_names(),
    )
    assert 'Environment="TEST_RUNTIME_SSH_HOST=user@host.example"\n' in service
    assert (
        'Environment="TEST_LEDGER_APPEND_COMMAND=onex-ledger-append --bus"\n' in service
    )
    assert 'Environment="TEST_LEDGER_HOST_NAME=h-test"\n' in service
    assert 'Environment="ONEX_SKILL_OVERLAY_ROOTS=/srv/overlays"\n' in service
    assert "UNRELATED_SECRET" not in service


def test_failing_control_a_value_a_unit_line_cannot_carry_refuses_the_render() -> None:
    with pytest.raises(ValueError, match="cannot carry"):
        sched.render_systemd(
            sched.load_schedule(),
            omni_root=ROOT,
            home=HOME,
            python=PYTHON,
            environment={"ONEX_HOST_NAME": 'bad"value'},
        )
    with pytest.raises(ValueError, match="cannot carry"):
        sched.render_systemd(
            sched.load_schedule(),
            omni_root=Path("/srv/omni root"),
            home=HOME,
            python=PYTHON,
            environment={},
        )


def test_delegation_events_defect_scan_install_writes_the_units_and_enables_the_timer_and_uninstall_rolls_back(
    tmp_path: Path,
) -> None:
    schedule = sched.load_schedule()
    recorder = Recorder()
    code = sched.install(
        schedule,
        omni_root=tmp_path / "omni",
        home=tmp_path,
        python=PYTHON,
        runner=recorder,
        environment={},
    )
    assert code == 0
    service_path, timer_path = sched.unit_paths(schedule, tmp_path)
    assert service_path.is_file()
    assert timer_path.is_file()
    assert (tmp_path / "omni" / schedule.state_directory).is_dir()
    assert recorder.commands == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", f"{schedule.unit_name}.timer"],
    ]
    undo = Recorder()
    assert sched.uninstall(schedule, home=tmp_path, runner=undo) == 0
    assert not service_path.exists()
    assert not timer_path.exists()
    assert undo.commands[0] == [
        "systemctl",
        "--user",
        "disable",
        "--now",
        f"{schedule.unit_name}.timer",
    ]


def test_failing_control_a_failing_enable_is_the_install_exit_code(
    tmp_path: Path,
) -> None:
    code = sched.install(
        sched.load_schedule(),
        omni_root=tmp_path / "omni",
        home=tmp_path,
        python=PYTHON,
        runner=Recorder(fail_on="enable"),
        environment={},
    )
    assert code == 5


def test_delegation_events_defect_scan_the_unit_carries_the_workspace_config_root_the_bus_append_needs() -> (
    None
):
    """Found on the lab: a unit without it ran the scan and then failed to append (bus exit 69)."""
    schedule = sched.load_schedule()
    assert "ONEX_WORKSPACE_CONFIG_ROOT" in schedule.unit_environment
    service, _ = sched.render_systemd(
        schedule,
        omni_root=ROOT,
        home=HOME,
        python=PYTHON,
        environment={"ONEX_WORKSPACE_CONFIG_ROOT": "/srv/config"},
    )
    assert 'Environment="ONEX_WORKSPACE_CONFIG_ROOT=/srv/config"\n' in service
