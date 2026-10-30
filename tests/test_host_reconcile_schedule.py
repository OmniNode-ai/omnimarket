# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The host reconcile node owns the schedule that keeps every reconciling host converged.

The contract declares the cadence, the node overlay declares the hosts, and a host that declares
the reconciler with no scheduler fails by name. The tests name no real host: the hosts here are
fixtures, and a ratchet fails when the package itself names one.
"""

from __future__ import annotations

import re
import shlex
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.models.model_host_reconcile import ModelHostReconcileCommand
from omnimarket.models.model_host_reconcile_schedule import (
    ModelHostReconcileHostRow,
    ModelHostReconcileSchedule,
    ModelHostReconcileScheduleOverlay,
)
from omnimarket.models.overlay_keys import ModelLabHostRow, ModelLabHostsTable
from omnimarket.nodes.node_host_reconcile_effect import __main__ as node_main
from omnimarket.nodes.node_host_reconcile_effect.handlers import (
    adapter_host_reconcile_schedule as schedule_module,
)
from omnimarket.nodes.node_host_reconcile_effect.handlers.adapter_host_reconcile_schedule import (
    HostReconcileScheduler,
    load_overlay,
    load_schedule,
    render_units,
    schedule_gaps,
)

pytestmark = pytest.mark.unit

NODE_DIR = Path(schedule_module.__file__).resolve().parents[1]
TIMER = "onex-host-reconcile.timer"


def row(
    name: str, scheduler: str | None = "systemd-user", by: str | None = None
) -> ModelHostReconcileHostRow:
    return ModelHostReconcileHostRow.model_validate(
        {"name": name, "scheduler": scheduler, "scheduled_by": by}
    )


def overlay(*rows: ModelHostReconcileHostRow) -> ModelHostReconcileScheduleOverlay:
    return ModelHostReconcileScheduleOverlay(hosts=rows)


def lab_table(*names: str, unavailable: tuple[str, ...] = ()) -> ModelLabHostsTable:
    return ModelLabHostsTable(
        hosts=tuple(
            ModelLabHostRow(name=name, target=name, available=name not in unavailable)
            for name in (*names, *unavailable)
        )
    )


class Systemctl:
    """A stand-in for the host's commands that records each call and answers by prefix."""

    def __init__(self, fail: str = "", listed: bool = True) -> None:
        self.calls: list[list[str]] = []
        self.fail = fail
        self.listed = listed

    def __call__(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(command))
        text = " ".join(command)
        if command[0].endswith("python"):
            return subprocess.CompletedProcess(list(command), 0, "", "")
        if self.fail and self.fail in text:
            return subprocess.CompletedProcess(list(command), 5, "", "refused\n")
        if "list-timers" in text:
            out = f"NEXT  LEFT  {TIMER}\n" if self.listed else "0 timers listed.\n"
            return subprocess.CompletedProcess(list(command), 0, out, "")
        if "loginctl" in text:
            return subprocess.CompletedProcess(list(command), 0, "Linger=yes\n", "")
        if "show" in text:
            return subprocess.CompletedProcess(
                list(command), 0, "Result=success\nExecMainStatus=0\n", ""
            )
        return subprocess.CompletedProcess(list(command), 0, "", "")

    def systemctl(self) -> list[list[str]]:
        return [c[2:] for c in self.calls if c[:2] == ["systemctl", "--user"]]


@pytest.fixture
def world(tmp_path: Path) -> tuple[Path, Path]:
    """A registry root with a dispatch venv interpreter, and a home."""
    root = tmp_path / "registry"
    python = root / ".onex-dispatch-venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n')
    python.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    return root, home


def handler(
    home: Path, run: Systemctl, *rows: ModelHostReconcileHostRow
) -> HostReconcileScheduler:
    return HostReconcileScheduler(
        schedule=load_schedule(), overlay=overlay(*rows), home=home, run=run
    )


def test_the_contract_declares_an_hourly_repair_schedule() -> None:
    schedule = load_schedule()
    assert schedule.mode == "repair"
    assert re.fullmatch(r"\*-\*-\* \*:\d\d:00", schedule.cadence.on_calendar), (
        "the cadence is once an hour"
    )
    assert schedule.cadence.persistent, (
        "a tick missed while the host was off still runs"
    )
    assert set(schedule.schedulers) == {"systemd-user", "launchd", "external"}
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    assert schedule.timeout_seconds * 1000 > contract["descriptor"]["timeout_ms"], (
        "the unit's bound must outlast the run's own bound"
    )


def test_the_package_names_no_host() -> None:
    """Host names are deployment facts: they live in the overlay, never in the node."""
    named = [
        f"{path.relative_to(NODE_DIR)}: {match.group()}"
        for path in sorted(NODE_DIR.rglob("*"))
        if path.suffix in {".py", ".yaml"} and path.is_file()
        for match in re.finditer(
            r"\bh\d{3}\b|\b\d{1,3}(?:\.\d{1,3}){3}\b", path.read_text()
        )
    ]
    assert not named


def test_a_host_that_declares_the_reconciler_with_no_scheduler_fails_naming_the_host() -> (
    None
):
    """The planted test: the guarantee that every reconciling host has a scheduler."""
    schedule = load_schedule()
    good = overlay(row("alpha"), row("bravo"))
    assert schedule_gaps(schedule, good) == ()
    planted = overlay(row("alpha"), row("charlie", scheduler=None))
    gaps = schedule_gaps(schedule, planted)
    assert len(gaps) == 1
    assert gaps[0].startswith("charlie: declares the reconciler with no scheduler")
    assert "alpha" not in gaps[0]


def test_every_scheduler_kind_but_the_installed_one_names_who_runs_it() -> None:
    schedule = load_schedule()
    gaps = schedule_gaps(
        schedule,
        overlay(
            row("alpha", "launchd"),
            row("bravo", "external", by="a-root.timer"),
            row("charlie", "systemd-user", by="a-root.timer"),
        ),
    )
    assert [gap.split(":")[0] for gap in gaps] == ["alpha", "charlie"]
    assert "must name scheduled_by" in gaps[0]


def test_a_host_declared_twice_is_a_gap() -> None:
    gaps = schedule_gaps(load_schedule(), overlay(row("alpha"), row("alpha")))
    assert gaps == ("alpha: declared more than once",)


def test_the_lab_host_table_and_the_overlay_must_agree() -> None:
    schedule = load_schedule()
    table = lab_table("alpha", "bravo", unavailable=("delta",))
    gaps = schedule_gaps(schedule, overlay(row("alpha"), row("echo")), table)
    assert gaps == (
        "bravo: runs lab work but declares no reconciler",
        "echo: names a host the lab host table does not have",
    )
    # an unavailable host may declare the reconciler, and is not required to
    assert (
        schedule_gaps(
            schedule, overlay(row("alpha"), row("bravo"), row("delta")), table
        )
        == ()
    )


def test_the_overlay_rejects_an_unknown_scheduler_and_an_unknown_field() -> None:
    with pytest.raises(ValueError, match="scheduler"):
        row("alpha", "cron")
    with pytest.raises(ValueError, match="bogus"):
        ModelHostReconcileHostRow.model_validate({"name": "alpha", "bogus": 1})


def test_the_rendered_unit_runs_the_node_in_repair_mode(
    world: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, home = world
    schedule = load_schedule()
    venv = root / ".onex-dispatch-venv"
    service, timer = render_units(schedule, omni_root=root, venv=venv, home=home)
    exec_line = next(
        line for line in service.splitlines() if line.startswith("ExecStart=")
    )
    words = shlex.split(exec_line.removeprefix("ExecStart="))
    assert words[:3] == [
        str(venv / "bin/python"),
        "-m",
        "omnimarket.nodes.node_host_reconcile_effect",
    ]
    assert "--check" not in words
    assert words[3:] == ["--omni-home", str(root), "--branch", schedule.branch]
    assert f"TimeoutStartSec={schedule.timeout_seconds}" in service
    assert f"OnCalendar={schedule.cadence.on_calendar}" in timer
    assert f"Unit={schedule.unit_name}.service" in timer
    assert f'Environment="ONEX_DISPATCH_VENV={venv}"' in service

    # The argument list the unit passes, fed to the node's own entry point, requests repair.
    seen: list[ModelHostReconcileCommand] = []

    class Result:
        exit_code = 0
        diagnostics: tuple[str, ...] = ()

        def model_dump_json(self) -> str:
            return "{}"

    class Capture:
        def __init__(self, **_: object) -> None: ...

        def handle(self, command: ModelHostReconcileCommand) -> Result:
            seen.append(command)
            return Result()

    monkeypatch.setattr(node_main, "HandlerHostReconcileEffect", Capture)
    assert node_main.main(words[3:]) == 0
    assert [command.mode for command in seen] == ["repair"]
    assert seen[0].branch == schedule.branch


def test_a_path_a_unit_line_cannot_carry_is_refused(world: tuple[Path, Path]) -> None:
    root, home = world
    with pytest.raises(ValueError, match="registry root"):
        render_units(
            load_schedule(),
            omni_root=root / "has space",
            venv=root / ".onex-dispatch-venv",
            home=home,
        )


def test_install_writes_both_units_and_enables_the_timer(
    world: tuple[Path, Path],
) -> None:
    root, home = world
    run = Systemctl()
    result = handler(home, run, row("alpha")).install("alpha", omni_root=root)
    assert result.exit_code == 0, result.stderr
    unit_dir = home / ".config/systemd/user"
    assert (unit_dir / "onex-host-reconcile.service").is_file()
    assert (unit_dir / TIMER).is_file()
    assert run.systemctl() == [
        ["daemon-reload"],
        ["enable", "--now", TIMER],
        ["list-timers", TIMER, "--no-pager"],
    ]
    assert (root / ".onex_state/host-reconcile").is_dir()
    assert f"installed {TIMER}" in result.stdout


def test_a_second_install_changes_no_unit_file(world: tuple[Path, Path]) -> None:
    root, home = world
    subject = handler(home, Systemctl(), row("alpha"))
    assert subject.install("alpha", omni_root=root).exit_code == 0
    again = subject.install("alpha", omni_root=root)
    assert again.exit_code == 0
    assert "onex-host-reconcile.service unchanged" in again.stdout
    assert f"{TIMER} unchanged" in again.stdout
    assert "wrote" not in again.stdout


def test_install_replaces_a_unit_that_ran_another_command(
    world: tuple[Path, Path],
) -> None:
    """The unit already on a host is the same name; the node's install takes it over."""
    root, home = world
    unit = home / ".config/systemd/user/onex-host-reconcile.service"
    unit.parent.mkdir(parents=True)
    unit.write_text("[Service]\nExecStart=/old/onex-host-reconcile\n")
    assert (
        handler(home, Systemctl(), row("alpha"))
        .install("alpha", omni_root=root)
        .exit_code
        == 0
    )
    assert "-m omnimarket.nodes.node_host_reconcile_effect" in unit.read_text()
    assert "/old/" not in unit.read_text()


def test_install_refuses_while_any_host_lacks_a_scheduler(
    world: tuple[Path, Path],
) -> None:
    root, home = world
    run = Systemctl()
    result = handler(home, run, row("alpha"), row("charlie", None)).install(
        "alpha", omni_root=root
    )
    assert result.exit_code == 1
    assert "GAP charlie: declares the reconciler with no scheduler" in result.stderr
    assert run.calls == []
    assert not (home / ".config").exists()


def test_install_refuses_a_host_the_overlay_does_not_declare(
    world: tuple[Path, Path],
) -> None:
    root, home = world
    result = handler(home, Systemctl(), row("alpha")).install("zulu", omni_root=root)
    assert result.exit_code == 1
    assert "zulu: not in the" in result.stderr


def test_a_host_scheduled_by_another_owner_installs_nothing(
    world: tuple[Path, Path],
) -> None:
    root, home = world
    run = Systemctl()
    result = handler(home, run, row("alpha", "external", by="a-root.timer")).install(
        "alpha", omni_root=root
    )
    assert result.exit_code == 0
    assert "a-root.timer" in result.stdout
    assert run.calls == []


def test_install_fails_when_the_reconciler_would_not_start(
    world: tuple[Path, Path],
) -> None:
    root, home = world
    (root / ".onex-dispatch-venv/bin/python").unlink()
    result = handler(home, Systemctl(), row("alpha")).install("alpha", omni_root=root)
    assert result.exit_code == 1
    assert "no dispatch venv interpreter" in result.stderr


def test_install_fails_when_systemd_lists_no_schedule(world: tuple[Path, Path]) -> None:
    root, home = world
    result = handler(home, Systemctl(listed=False), row("alpha")).install(
        "alpha", omni_root=root
    )
    assert result.exit_code == 1
    assert "lists no schedule" in result.stderr


def test_install_reports_a_failing_systemctl_with_its_status(
    world: tuple[Path, Path],
) -> None:
    root, home = world
    result = handler(home, Systemctl(fail="enable"), row("alpha")).install(
        "alpha", omni_root=root
    )
    assert result.exit_code == 5


def test_status_reports_the_timer_linger_and_last_result(
    world: tuple[Path, Path],
) -> None:
    _, home = world
    result = handler(home, Systemctl(), row("alpha")).status("alpha")
    assert result.exit_code == 0, result.stdout
    assert f"LOADED {TIMER}" in result.stdout
    assert "LINGER yes" in result.stdout
    assert "Result=success" in result.stdout
    inactive = handler(home, Systemctl(fail="is-active"), row("alpha")).status("alpha")
    assert inactive.exit_code == 1
    assert f"NOT LOADED {TIMER}" in inactive.stdout


def test_uninstall_removes_the_units_and_run_now_starts_the_service(
    world: tuple[Path, Path],
) -> None:
    root, home = world
    run = Systemctl()
    subject = handler(home, run, row("alpha"))
    assert subject.install("alpha", omni_root=root).exit_code == 0
    assert subject.run_now("alpha").exit_code == 0
    assert ["start", "onex-host-reconcile.service"] in run.systemctl()
    assert subject.uninstall("alpha").exit_code == 0
    assert not (home / ".config/systemd/user" / TIMER).exists()
    assert (root / ".onex_state/host-reconcile").is_dir(), "logs are left in place"


def write_overlay(root: Path, hosts: list[dict[str, object]]) -> Path:
    path = root / "node_host_reconcile_effect/overlay.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(yaml.safe_dump({"hosts": hosts}))
    return root


def test_check_through_the_entry_point_names_the_gap_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv(
        "ONEX_SKILL_OVERLAY_ROOTS",
        str(
            write_overlay(
                tmp_path,
                [{"name": "alpha", "scheduler": "systemd-user"}, {"name": "charlie"}],
            )
        ),
    )
    assert node_main.main(["schedule", "check"]) == 1
    assert (
        "GAP charlie: declares the reconciler with no scheduler"
        in capsys.readouterr().err
    )


def test_check_passes_when_every_host_has_a_scheduler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv(
        "ONEX_SKILL_OVERLAY_ROOTS",
        str(write_overlay(tmp_path, [{"name": "alpha", "scheduler": "systemd-user"}])),
    )
    assert node_main.main(["schedule", "check"]) == 0
    assert "OK 1 hosts" in capsys.readouterr().out


def test_check_against_the_lab_table_reads_the_pinned_table(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    roots = write_overlay(
        tmp_path / "overlays", [{"name": "alpha", "scheduler": "systemd-user"}]
    )
    table = tmp_path / "hosts.yaml"
    table.write_text(
        yaml.safe_dump(
            {
                "hosts": [
                    {"name": "alpha", "target": "a"},
                    {"name": "bravo", "target": "b"},
                ]
            }
        )
    )
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(roots))
    monkeypatch.setenv("ONEX_LAB_RUN_HOSTS", str(table))
    assert node_main.main(["schedule", "check", "--against-lab-table"]) == 1
    assert (
        "GAP bravo: runs lab work but declares no reconciler" in capsys.readouterr().err
    )
    assert node_main.main(["schedule", "check"]) == 0


def test_a_missing_overlay_refuses_and_says_how_to_supply_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    with pytest.raises(NodeOverlayError, match="node_host_reconcile_effect"):
        load_overlay()
    assert node_main.main(["schedule", "check"]) == 3
    assert "node_host_reconcile_effect/overlay.yaml" in capsys.readouterr().err


def test_install_dry_run_through_the_entry_point_renders_and_writes_nothing(
    world: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, home = world
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("ONEX_DISPATCH_VENV", raising=False)
    monkeypatch.setenv(
        "ONEX_SKILL_OVERLAY_ROOTS",
        str(
            write_overlay(
                tmp_path / "overlays", [{"name": "alpha", "scheduler": "systemd-user"}]
            )
        ),
    )
    argv = [
        "schedule",
        "install",
        "--host",
        "alpha",
        "--omni-home",
        str(root),
        "--dry-run",
    ]
    assert node_main.main(argv) == 0
    out = capsys.readouterr().out
    assert "ExecStart=" in out
    assert "OnCalendar=" in out
    assert not (home / ".config").exists()
    monkeypatch.delenv("OMNI_HOME", raising=False)
    assert node_main.main(["schedule", "install", "--host", "alpha"]) == 3


def test_the_plain_entry_point_still_rejects_an_unknown_argument(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert node_main.main(["--bogus"]) == 3
    assert "INDETERMINATE" in capsys.readouterr().err


def test_the_schedule_block_rejects_a_check_mode() -> None:
    block = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())["schedule"]
    block["mode"] = "check"
    with pytest.raises(ValueError, match="mode"):
        ModelHostReconcileSchedule.model_validate(block)
