# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A transient unit whose environment file is gone is skipped, not a snapshot failure (OMN-24901).

Recorded case: on 2026-11-18 the h051 worktree reconcile tick exited 1 with failures=22 of scanned=22
because three failed transient units (run-u1046, run-u1047, run-u1048: `/bin/true` with
`EnvironmentFile=/tmp/tmp.qNabOMVcM6/k.env`, a file that no longer exists) made the unit reader raise
UnitSnapshotError, which kept and failed every tree. A worktree unit, an installed unit and an active
unit with a missing required environment file still raise.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

from omnimarket.nodes.node_worktree_reconcile_effect.handlers.adapter_units import (
    SystemdUnitRootReader,
    UnitSnapshotError,
)
from tests.test_worktree_reconcile_effect import (
    FakeMergedPrReader,
    probe_for,
    run_with,
    squash_merged_then_moved_on,
)

pytestmark = pytest.mark.unit

GONE = "/tmp/tmp.qNabOMVcM6/k.env"
KEPT_TREE = "/srv/registry/omni_worktrees/OMN-1/omnimarket"


@dataclass(frozen=True)
class _Unit:
    name: str
    state: str
    text: str
    active: str = "failed"


def _transient(name: str, env: str, *, exec_start: str = "/bin/true") -> str:
    return (
        f"# /run/user/1000/systemd/transient/{name}\n"
        "# This is a transient unit file, created programmatically via the systemd API.\n"
        f'[Service]\nEnvironmentFile={env}\nExecStart="{exec_start}"\n'
    )


def _installed(name: str, env: str) -> str:
    return (
        f"# /etc/systemd/user/{name}\n"
        f"[Service]\nEnvironmentFile={env}\nExecStart=/bin/true\n"
    )


class _Systemctl:
    """A scripted systemctl for the user manager: listing with states, cat text and ActiveState."""

    def __init__(self, *units: _Unit) -> None:
        self.units = {u.name: u for u in units}
        self.commands: list[list[str]] = []

    def __call__(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.commands.append(list(command))
        scope, verb = command[1], command[2]
        if scope == "--system":
            return subprocess.CompletedProcess(list(command), 0, "", "")
        if verb == "list-unit-files":
            out = "".join(f"{u.name} {u.state} -\n" for u in self.units.values())
            return subprocess.CompletedProcess(list(command), 0, out, "")
        if verb == "cat":
            out = "".join(self.units[n].text for n in command[3:])
            return subprocess.CompletedProcess(list(command), 0, out, "")
        if verb == "show":
            unit = self.units[command[3]]
            return subprocess.CompletedProcess(list(command), 0, unit.active + "\n", "")
        raise AssertionError(f"unexpected systemctl call {command}")


def test_a_failed_transient_unit_with_a_gone_environment_file_is_skipped_with_the_reason() -> (
    None
):
    transient = _Unit(
        "run-u1046.service", "transient", _transient("run-u1046.service", GONE)
    )
    systemctl = _Systemctl(transient, _Unit("onex-plain.service", "static", _plain()))
    roots = SystemdUnitRootReader(run=systemctl).read()
    assert [(s.unit, s.reason, s.path) for s in roots.skipped] == [
        ("run-u1046.service", "environment_file_missing", GONE)
    ]
    # The scan completed: the other unit was still read.
    assert roots.units_read == 1
    assert roots.naming(f"{KEPT_TREE}") == "onex-plain.service"


def _plain() -> str:
    return (
        "# /etc/systemd/user/onex-plain.service\n"
        f"[Service]\nExecStart={KEPT_TREE}/.venv/bin/python\n"
    )


def test_three_failed_transient_units_are_each_skipped_and_the_scan_finishes() -> None:
    units = tuple(
        _Unit(f"run-u{n}.service", "transient", _transient(f"run-u{n}.service", GONE))
        for n in (1046, 1047, 1048)
    )
    roots = SystemdUnitRootReader(
        run=_Systemctl(*units, _Unit("onex-plain.service", "static", _plain()))
    ).read()
    assert [s.unit for s in roots.skipped] == [
        "run-u1046.service",
        "run-u1047.service",
        "run-u1048.service",
    ]
    assert {s.reason for s in roots.skipped} == {"environment_file_missing"}


def test_an_installed_unit_with_a_gone_environment_file_still_raises() -> None:
    installed = _Unit(
        "onex-installed.service",
        "enabled",
        _installed("onex-installed.service", GONE),
        active="failed",
    )
    with pytest.raises(UnitSnapshotError):
        SystemdUnitRootReader(run=_Systemctl(installed)).read()


def test_a_worktree_unit_with_a_gone_environment_file_still_raises() -> None:
    worktree_unit = _Unit(
        "run-u2000.service",
        "transient",
        _transient(
            "run-u2000.service",
            GONE,
            exec_start=f"{KEPT_TREE}/.venv/bin/python",
        ),
    )
    with pytest.raises(UnitSnapshotError):
        SystemdUnitRootReader(run=_Systemctl(worktree_unit)).read()


def test_a_transient_unit_whose_environment_file_lies_in_a_worktree_still_raises() -> (
    None
):
    in_tree = _Unit(
        "run-u2001.service",
        "transient",
        _transient("run-u2001.service", f"{KEPT_TREE}/k.env"),
    )
    with pytest.raises(UnitSnapshotError):
        SystemdUnitRootReader(run=_Systemctl(in_tree)).read()


def test_an_active_transient_unit_with_a_gone_environment_file_still_raises() -> None:
    running = _Unit(
        "run-u3000.service",
        "transient",
        _transient("run-u3000.service", GONE),
        active="active",
    )
    with pytest.raises(UnitSnapshotError):
        SystemdUnitRootReader(run=_Systemctl(running)).read()


def test_a_failed_transient_unit_whose_state_cannot_be_read_still_raises() -> None:
    transient = _Unit(
        "run-u1046.service", "transient", _transient("run-u1046.service", GONE)
    )
    systemctl = _Systemctl(transient)

    def broken_show(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        if command[2] == "show":
            return subprocess.CompletedProcess(list(command), 1, "", "")
        return systemctl(command)

    with pytest.raises(UnitSnapshotError):
        SystemdUnitRootReader(run=broken_show).read()


def test_the_run_report_carries_the_skipped_units_and_counts_no_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run chain: the tick that failed 22 of 22 completes with failures=0 and names what it skipped."""
    root, _path, head = squash_merged_then_moved_on(tmp_path)
    reader = FakeMergedPrReader(frozenset({head}))
    transient = _Unit(
        "run-u1046.service", "transient", _transient("run-u1046.service", GONE)
    )
    units = SystemdUnitRootReader(run=_Systemctl(transient))
    result = run_with(probe_for(reader, monkeypatch, units=units), root, monkeypatch)
    row = result.decided_events[0]
    assert "unit_snapshot_failed" not in row.facts.probe_errors
    assert result.completed_event.failures == 0
    assert [(s.unit, s.reason, s.path) for s in result.skipped_units] == [
        ("run-u1046.service", "environment_file_missing", GONE)
    ]
