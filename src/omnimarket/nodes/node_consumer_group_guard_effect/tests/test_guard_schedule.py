# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The guard owns its timer; deployment facts come only from fixture overlays."""

from __future__ import annotations

import json
import re
import shlex
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_consumer_group_guard_effect import (
    __main__ as node_main,
)
from omnimarket.nodes.node_consumer_group_guard_effect import (
    runtime_consumer_group_guard_schedule as schedule_cli,
)
from omnimarket.nodes.node_consumer_group_guard_effect.guard_schedule import (
    CONTRACT_PATH,
    NODE_MODULE,
    GuardScheduler,
    judge_census,
    load_overlay,
    load_schedule,
    render_units,
    schedule_gaps,
)
from omnimarket.nodes.node_consumer_group_guard_effect.guard_schedule_models import (
    ModelGuardHostRow,
    ModelGuardSchedule,
    ModelGuardScheduleOverlay,
)

pytestmark = pytest.mark.unit

TIMER = "onex-consumer-group-guard.timer"
SERVICE = "onex-consumer-group-guard.service"
NOW = datetime(2026, 11, 3, 12, tzinfo=UTC)


def row(
    name: str = "h053",
    *,
    bus_lane: str = "dev",
    workspace_config_root: str = "/srv/fixture/config",
) -> ModelGuardHostRow:
    return ModelGuardHostRow(
        name=name,
        scheduler="systemd-user",
        bus_lane=bus_lane,
        workspace_config_root=workspace_config_root,
    )


class Systemctl:
    """Record commands and provide scripted failures without calling systemd."""

    def __init__(self, *, fail: str = "", listed: bool = True) -> None:
        self.calls: list[list[str]] = []
        self.fail = fail
        self.listed = listed

    def __call__(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(command))
        if self.fail and any(self.fail in arg for arg in command):
            return subprocess.CompletedProcess(list(command), 5, "", "refused\n")
        output = ""
        if "list-timers" in command:
            output = f"NEXT LEFT {TIMER}\n" if self.listed else "0 timers listed.\n"
        return subprocess.CompletedProcess(list(command), 0, output, "")

    def systemctl(self) -> list[list[str]]:
        return [call[2:] for call in self.calls if call[:2] == ["systemctl", "--user"]]


@pytest.fixture
def world(tmp_path: Path) -> tuple[Path, Path]:
    root, home = tmp_path / "registry", tmp_path / "home"
    python = root / ".onex-dispatch-venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\nexit 0\n")
    python.chmod(0o755)
    home.mkdir()
    return root, home


def subject(home: Path, run: Systemctl, *rows: ModelGuardHostRow) -> GuardScheduler:
    return GuardScheduler(
        schedule=load_schedule(),
        overlay=ModelGuardScheduleOverlay(hosts=rows or (row(),)),
        home=home,
        run=run,
        now=lambda: NOW,
    )


def write_overlay(root: Path, *rows: ModelGuardHostRow) -> Path:
    path = root / "node_consumer_group_guard_effect/overlay.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(yaml.safe_dump({"hosts": [r.model_dump() for r in rows]}))
    return root


def census(
    *,
    detail: str = "reaped 3 of 3 eligible Empty groups",
    age: int = 0,
    status: str = "OK",
) -> str:
    return json.dumps(
        {
            "observed_at": (NOW - timedelta(seconds=age)).isoformat(),
            "status": status,
            "reap_detail": detail,
        }
    )


def write_state(root: Path, text: str, *, filename: str = "census.json") -> Path:
    path = root / load_schedule().state_directory / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_guard_schedule_render(
    world: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, home = world
    overlay_root = write_overlay(tmp_path / "overlays", row())
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(overlay_root))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("ONEX_DISPATCH_VENV", raising=False)
    run = Systemctl()
    monkeypatch.setattr(
        schedule_cli,
        "GuardScheduler",
        lambda **kwargs: GuardScheduler(**kwargs, run=run),
    )
    assert (
        schedule_cli.main(
            ["install", "--host", "h053", "--omni-home", str(root), "--dry-run"]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "[Service]" in out
    assert "[Timer]" in out
    state = root / load_schedule().state_directory
    python = root / ".onex-dispatch-venv/bin/python"
    assert f'ExecStart="{python}" -m {NODE_MODULE} --state-dir "{state}"\n' in out
    assert "--omni-home" not in out
    assert "--bus-lane" not in out
    assert 'Environment="ONEX_CONSUMER_GROUP_GUARD_BUS_LANE=dev"' in out
    assert 'Environment="ONEX_WORKSPACE_CONFIG_ROOT=/srv/fixture/config"' in out
    assert f'Environment="HOME={home}"' in out
    assert f'Environment="PATH={home}/.local/bin:/usr/local/bin:/usr/bin:/bin"' in out
    assert f"WorkingDirectory={root}\n" in out
    assert "Type=oneshot\n" in out
    assert "TimeoutStartSec" not in out
    assert f"StandardOutput=append:{state}/consumer-group-guard.out.log\n" in out
    assert f"StandardError=append:{state}/consumer-group-guard.err.log\n" in out
    assert "OnActiveSec=1s\n" in out
    assert "OnUnitActiveSec=300s\n" in out
    assert "AccuracySec=1s\nRandomizedDelaySec=0\n" in out
    assert f"Unit={SERVICE}\n" in out
    assert "WantedBy=timers.target\n" in out
    assert not (home / ".config").exists()
    assert not state.exists()
    assert run.calls == []


@pytest.mark.parametrize("duplicate", [False, True])
def test_guard_schedule_unlisted_host(
    world: tuple[Path, Path], duplicate: bool
) -> None:
    root, home = world
    run = Systemctl()
    rows = (row(), row()) if duplicate else (row("another-host"),)
    scheduler = subject(home, run, *rows)
    result = scheduler.install("h053", omni_root=root)
    assert result.exit_code == 1
    assert "h053" in result.stderr
    if duplicate:
        assert "GAP h053: declared more than once" in result.stderr
        assert scheduler.check().exit_code == 1
    else:
        assert result.stderr == (
            "h053: not in the node_consumer_group_guard_effect overlay, "
            "so no scheduler is declared for it\n"
        )
    assert run.calls == []
    assert not (home / ".config").exists()
    assert not (root / load_schedule().state_directory).exists()


def test_guard_schedule_values_from_overlay(world: tuple[Path, Path]) -> None:
    root, home = world
    original = subject(home, Systemctl()).install("h053", omni_root=root, dry_run=True)
    changed = subject(
        home,
        Systemctl(),
        row(bus_lane="fixture-lane", workspace_config_root="/other/config"),
    ).install("h053", omni_root=root, dry_run=True)
    assert (
        'Environment="ONEX_CONSUMER_GROUP_GUARD_BUS_LANE=fixture-lane"'
        in changed.stdout
    )
    assert 'Environment="ONEX_WORKSPACE_CONFIG_ROOT=/other/config"' in changed.stdout
    assert "/srv/fixture/config" in original.stdout
    assert "/srv/fixture/config" not in changed.stdout
    refused = subject(home, Systemctl(), row("other-host")).install(
        "h053", omni_root=root
    )
    assert refused.exit_code == 1


def test_guard_schedule_render_values_from_contract(world: tuple[Path, Path]) -> None:
    root, home = world
    block = load_schedule().model_dump()
    block.update(
        unit_name="fixture-guard", dispatch_venv="venv", state_directory="state"
    )
    block["cadence"] = {
        "first_pass": "2s",
        "interval_seconds": 42,
        "accuracy": "3s",
        "description": "fixture cadence",
    }
    schedule = ModelGuardSchedule.model_validate(block)
    service, timer = render_units(
        schedule,
        row(),
        venv=root / schedule.dispatch_venv,
        omni_root=root,
        home=home,
    )
    assert f"WorkingDirectory={root}\n" in service
    assert f'ExecStart="{root}/venv/bin/python"' in service
    assert f'--state-dir "{root}/state"' in service
    assert "SyslogIdentifier=fixture-guard\n" in service
    assert "# fixture cadence\n" in timer
    assert "OnActiveSec=2s\nOnUnitActiveSec=42s\nAccuracySec=3s\n" in timer
    assert "Unit=fixture-guard.service\n" in timer


def test_guard_schedule_render_quotes_overlay_environment(
    world: tuple[Path, Path],
) -> None:
    root, home = world
    service, _ = render_units(
        load_schedule(),
        row(workspace_config_root='/srv/a "quoted" 50%/$config'),
        venv=root / "venv",
        omni_root=root,
        home=home,
    )
    assert (
        r'Environment="ONEX_WORKSPACE_CONFIG_ROOT=/srv/a \"quoted\" 50%%/$config"'
        in service
    )
    line = next(line for line in service.splitlines() if line.startswith("ExecStart="))
    assert shlex.split(line.removeprefix("ExecStart="))[1:3] == ["-m", NODE_MODULE]


def test_guard_schedule_install_and_repeat(world: tuple[Path, Path]) -> None:
    root, home = world
    run = Systemctl()
    scheduler = subject(home, run)
    result = scheduler.install("h053", omni_root=root)
    assert result.exit_code == 0, result.stderr
    assert f"installed {TIMER}" in result.stdout
    assert run.systemctl() == [
        ["daemon-reload"],
        ["enable", "--now", TIMER],
        ["list-timers", TIMER, "--no-pager"],
    ]
    assert (scheduler.unit_dir / SERVICE).is_file()
    assert (scheduler.unit_dir / TIMER).is_file()
    again = scheduler.install("h053", omni_root=root)
    assert again.exit_code == 0
    assert f"{SERVICE} unchanged" in again.stdout
    assert f"{TIMER} unchanged" in again.stdout
    assert "wrote" not in again.stdout


@pytest.mark.parametrize(
    "failure", ["missing-python", "import", "enable", "unlisted-timer"]
)
def test_guard_schedule_install_refuses_failed_prerequisites(
    world: tuple[Path, Path], failure: str
) -> None:
    root, home = world
    run = Systemctl(
        fail="import"
        if failure == "import"
        else "enable"
        if failure == "enable"
        else "",
        listed=failure != "unlisted-timer",
    )
    if failure == "missing-python":
        (root / ".onex-dispatch-venv/bin/python").unlink()
    result = subject(home, run).install("h053", omni_root=root)
    assert result.exit_code != 0
    assert result.stderr
    if failure in ("missing-python", "import"):
        assert not (home / ".config").exists()
        assert run.systemctl() == []


@pytest.mark.parametrize("loaded", [True, False])
@pytest.mark.parametrize(
    ("text", "healthy"),
    [
        (census(), True),
        (census(detail="nothing to reap among 5 Empty groups"), True),
        (census(detail="reap skipped, listing unreadable: KafkaException"), False),
        (census(detail="reap stopped at the first error: X"), False),
        (census(detail="reaping disabled by contract"), False),
        (census(detail="reaped 2 of 3 eligible Empty groups"), False),
        (census(detail=""), False),
        (None, False),
        (census(age=601), False),
        (census(status="FAIL"), False),
        ("{broken", False),
        ('{"status":"OK"}', False),
        ('{"observed_at":"nonsense"}', False),
    ],
)
def test_guard_schedule_status(
    world: tuple[Path, Path], loaded: bool, text: str | None, healthy: bool
) -> None:
    root, home = world
    if text is not None:
        write_state(root, text)
    run = Systemctl(fail="" if loaded else "is-active")
    result = subject(home, run).status("h053", omni_root=root)
    assert result.exit_code == (0 if loaded and healthy else 1)
    lines = result.stdout.splitlines()
    assert lines[0] == f"{'LOADED' if loaded else 'NOT LOADED'} {TIMER}"
    assert lines[-1].startswith("HEALTHY " if healthy else "NOT HEALTHY: ")
    assert run.systemctl() == [["is-active", TIMER]] + (
        [["list-timers", TIMER, "--no-pager"]] if loaded else []
    )


@pytest.mark.parametrize("newer_failure", [True, False])
def test_guard_schedule_status_ignores_last_run(
    world: tuple[Path, Path], newer_failure: bool
) -> None:
    root, home = world
    write_state(root, census(age=10), filename="last-run.json")
    if newer_failure:
        write_state(root, census(status="FAIL"))
    result = subject(home, Systemctl()).status("h053", omni_root=root)
    assert result.exit_code == 1
    assert "NOT HEALTHY: " in result.stdout
    assert (
        "status is 'FAIL'" if newer_failure else "census.json is absent"
    ) in result.stdout


def test_guard_schedule_status_unreadable_file(world: tuple[Path, Path]) -> None:
    root, home = world
    path = write_state(root, "")
    path.write_bytes(b"\xff")
    result = subject(home, Systemctl()).status("h053", omni_root=root)
    assert result.exit_code == 1
    assert "NOT HEALTHY: census.json is unreadable" in result.stdout


@pytest.mark.parametrize(
    ("text", "healthy"),
    [
        (census(age=600), True),
        (census(age=-10), True),
        (census(age=-120), False),
        ("[]", False),
        ('{"observed_at": 123}', False),
        ('{"observed_at":"2026-11-03T12:00:00"}', False),
    ],
)
def test_guard_schedule_status_judge_boundaries(text: str, healthy: bool) -> None:
    assert judge_census(text, NOW, 300)[0] is healthy


def test_guard_schedule_uninstall(world: tuple[Path, Path]) -> None:
    root, home = world
    run = Systemctl()
    scheduler = subject(home, run)
    scheduler.unit_dir.mkdir(parents=True)
    for name in (SERVICE, TIMER):
        (scheduler.unit_dir / name).write_text("old unit")
    state = write_state(root, census())
    log = state.with_name("consumer-group-guard.out.log")
    log.write_text("keep logs")
    result = scheduler.uninstall("h053")
    assert result.exit_code == 0
    assert run.calls == [
        ["systemctl", "--user", "disable", "--now", TIMER],
        ["systemctl", "--user", "daemon-reload"],
    ]
    assert all("stop" not in call and SERVICE not in call for call in run.calls)
    assert not (scheduler.unit_dir / SERVICE).exists()
    assert not (scheduler.unit_dir / TIMER).exists()
    assert state.is_file()
    assert log.read_text() == "keep logs"


def test_guard_schedule_run_now(world: tuple[Path, Path]) -> None:
    _, home = world
    run = Systemctl()
    assert subject(home, run).run_now("h053").exit_code == 0
    assert run.calls == [["systemctl", "--user", "start", SERVICE]]
    failed = subject(home, Systemctl(fail="start")).run_now("h053")
    assert failed.exit_code == 5
    assert "refused" in failed.stderr


@pytest.mark.parametrize("verb", ["status", "run-now", "uninstall"])
def test_guard_schedule_unlisted_host_other_verbs(
    world: tuple[Path, Path], verb: str
) -> None:
    root, home = world
    run = Systemctl()
    scheduler = subject(home, run)
    if verb == "status":
        result = scheduler.status("absent", omni_root=root)
    elif verb == "run-now":
        result = scheduler.run_now("absent")
    else:
        result = scheduler.uninstall("absent")
    assert result.exit_code == 1
    assert "absent" in result.stderr
    assert not run.calls


def test_guard_main_delegates_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str] | None] = []

    def record(argv: list[str] | None = None) -> int:
        calls.append(argv)
        return 73

    monkeypatch.setattr(
        "omnimarket.nodes.node_consumer_group_guard_effect.runtime_consumer_group_guard.main",
        record,
    )
    for argv in (["--state-dir", "X", "--bus-lane", "dev"], ["--state-dir", "X"], []):
        calls.clear()
        assert node_main.main(argv) == 73
        assert calls == [argv]
        assert calls[0] is argv
    calls.clear()
    schedule_calls: list[Sequence[str]] = []

    def schedule_record(argv: Sequence[str]) -> int:
        schedule_calls.append(argv)
        return 41

    monkeypatch.setattr(schedule_cli, "main", schedule_record)
    assert node_main.main(["schedule", "check"]) == 41
    assert schedule_calls == [["check"]]
    assert calls == []
    monkeypatch.setattr("sys.argv", ["guard", "--state-dir", "X"])
    assert node_main.main() == 73
    assert calls == [["--state-dir", "X"]]


def test_guard_schedule_contract_loads_without_host_names() -> None:
    schedule = load_schedule()
    assert schedule.cadence.interval_seconds == 300
    block = yaml.safe_load(CONTRACT_PATH.read_text())["schedule"]
    assert "hosts" not in block
    assert not re.search(r"\bh\d{3}\b", yaml.safe_dump(block))
    assert set(schedule.schedulers) == {"systemd-user"}


def test_guard_schedule_cli_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    overlay_root = write_overlay(tmp_path / "overlays", row())
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(overlay_root))
    assert load_overlay({"ONEX_SKILL_OVERLAY_ROOTS": str(overlay_root)}).hosts == (
        row(),
    )
    assert node_main.main(["schedule", "check"]) == 0
    assert "OK 1 hosts" in capsys.readouterr().out
    path = overlay_root / "node_consumer_group_guard_effect/overlay.yaml"
    path.write_text(yaml.safe_dump({"hosts": [row().model_dump(), row().model_dump()]}))
    assert node_main.main(["schedule", "check"]) == 1
    assert "GAP h053" in capsys.readouterr().err
    assert schedule_gaps(ModelGuardScheduleOverlay(hosts=(row(), row()))) == (
        "h053: declared more than once",
    )


@pytest.mark.parametrize(
    "document",
    [
        None,
        "hosts: []",
        "hosts: [broken]",
        "hosts: [",
        "hosts:\n - name: a\n   scheduler: unknown",
    ],
)
def test_guard_schedule_cli_overlay_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    document: str | None,
) -> None:
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    if document is not None:
        path = tmp_path / "node_consumer_group_guard_effect/overlay.yaml"
        path.parent.mkdir()
        path.write_text(document)
    assert schedule_cli.main(["check"]) == 3
    assert "INDETERMINATE:" in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["bogus"],
        ["install"],
        ["status"],
        ["check", "--bogus"],
        ["install", "--host", "h053"],
        ["status", "--host", "h053"],
    ],
)
def test_guard_schedule_cli_bad_arguments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
) -> None:
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(write_overlay(tmp_path, row())))
    monkeypatch.delenv("OMNI_HOME", raising=False)
    assert schedule_cli.main(argv) == 3
    assert "INDETERMINATE:" in capsys.readouterr().err


def test_guard_schedule_cli_environment_paths(
    world: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, home = world
    venv = tmp_path / "override-venv"
    python = venv / "bin/python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\nexit 0\n")
    python.chmod(0o755)
    monkeypatch.setenv(
        "ONEX_SKILL_OVERLAY_ROOTS", str(write_overlay(tmp_path / "overlays", row()))
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("OMNI_HOME", str(root))
    monkeypatch.setenv("ONEX_DISPATCH_VENV", str(venv))
    assert schedule_cli.main(["install", "--host", "h053", "--dry-run"]) == 0
    assert f'ExecStart="{python}"' in capsys.readouterr().out
    write_state(root, census())
    run = Systemctl()
    monkeypatch.setattr(
        schedule_cli,
        "GuardScheduler",
        lambda **kwargs: GuardScheduler(**kwargs, run=run, now=lambda: NOW),
    )
    assert schedule_cli.main(["status", "--host", "h053"]) == 0
    assert "HEALTHY " in capsys.readouterr().out


@pytest.mark.parametrize(
    "field", ["name", "scheduler", "bus_lane", "workspace_config_root"]
)
def test_guard_schedule_models_require_host_fields(field: str) -> None:
    data = row().model_dump()
    del data[field]
    with pytest.raises(ValidationError, match=field):
        ModelGuardHostRow.model_validate(data)


def test_guard_schedule_models_forbid_extras_and_are_frozen() -> None:
    field_name = "name"
    with pytest.raises(ValidationError, match="extra"):
        ModelGuardHostRow.model_validate({**row().model_dump(), "extra": "bad"})
    with pytest.raises(ValidationError, match="frozen"):
        setattr(row(), field_name, "changed")
    data = row().model_dump()
    data.update(name="  fixture  ", bus_lane=" dev ")
    assert ModelGuardHostRow.model_validate(data).name == "fixture"
    data["workspace_config_root"] = " "
    with pytest.raises(ValidationError, match="workspace_config_root"):
        ModelGuardHostRow.model_validate(data)
    block = load_schedule().model_dump()
    block["cadence"]["interval_seconds"] = 0
    with pytest.raises(ValidationError, match="interval_seconds"):
        ModelGuardSchedule.model_validate(block)
    block = load_schedule().model_dump()
    block["unit_name"] = "bad/name"
    with pytest.raises(ValidationError, match="unit_name"):
        ModelGuardSchedule.model_validate(block)
