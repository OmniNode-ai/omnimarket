# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A host with accumulated worktrees needs a recurring node tick, not a tmp reaper."""

import json
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest
import yaml

from omnimarket.events.worktree_reconcile import ModelWorktreeReconcileCommand
from omnimarket.nodes.node_worktree_reconcile_effect.handlers.handler_worktree_schedule import (
    HandlerWorktreeSchedule,
)
from omnimarket.nodes.node_worktree_reconcile_effect.models.model_worktree_schedule import (
    ModelWorktreeScheduleOverlay,
    ModelWorktreeScheduleRequest,
)


def overlay(tmp_path: Path) -> Path:
    root = tmp_path / "registry" / "trees"
    root.mkdir(parents=True)
    project = tmp_path / "project"
    entry = project / "src/omnimarket/nodes/node_worktree_reconcile_effect/__main__.py"
    entry.parent.mkdir(parents=True)
    entry.write_text(
        (
            Path(__file__).resolve().parents[1]
            / "src/omnimarket/nodes/node_worktree_reconcile_effect/__main__.py"
        ).read_text()
    )
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
    environment = tmp_path / "host.env"
    environment.write_text("KAFKA_BROKERS=localhost:9092\n")
    path = tmp_path / "overlay.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "python": "/usr/bin/python3",
                "project": str(project),
                "state_directory": str(tmp_path / "state"),
                "environment_file": str(environment),
                "hosts": {
                    "lab-host": json.loads(
                        ModelWorktreeReconcileCommand(
                            host="lab-host", roots=(str(root),), correlation_id=uuid4()
                        ).model_dump_json()
                    )
                },
            }
        )
    )
    return path


def test_accumulated_worktrees_have_a_recurring_host_node_tick(tmp_path: Path) -> None:
    """Recorded case: 205 GB of worktrees, with only a /tmp reaper installed."""
    path = overlay(tmp_path)
    result = HandlerWorktreeSchedule(unit_directory=tmp_path / "units").handle(
        ModelWorktreeScheduleRequest(
            mode="render", host="lab-host", overlay_path=str(path)
        )
    )
    assert result.exit_code == 0
    assert "OnUnitActiveSec=1800s" in result.timer
    assert "Persistent=true" in result.timer
    assert "omnimarket.nodes.node_worktree_reconcile_effect" in result.service
    assert "tick" in result.service
    assert str(path.resolve()) in result.service
    assert "EnvironmentFile=" in result.service
    assert "UnsetEnvironment=GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE" in result.service
    assert "--execute" not in result.service
    assert result.revision


def test_undeclared_host_refuses_before_writing(tmp_path: Path) -> None:
    path = overlay(tmp_path)
    units = tmp_path / "units"
    result = HandlerWorktreeSchedule(unit_directory=units).handle(
        ModelWorktreeScheduleRequest(
            mode="install", host="other", overlay_path=str(path)
        )
    )
    assert result.exit_code != 0
    assert "other" in result.stderr
    assert not units.exists()


def test_install_checks_enable_and_scheduling(tmp_path: Path) -> None:
    path = overlay(tmp_path)
    calls: list[list[str]] = []

    def run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        output = "active\n" if "is-active" in argv else "enabled\n"
        if "list-timers" in argv:
            output = "onex-worktree-reconcile.timer onex-worktree-reconcile.service\n"
        return subprocess.CompletedProcess(argv, 0, output, "")

    units = tmp_path / "units"
    result = HandlerWorktreeSchedule(unit_directory=units, runner=run).handle(
        ModelWorktreeScheduleRequest(
            mode="install", host="lab-host", overlay_path=str(path)
        )
    )
    assert result.exit_code == 0
    assert (units / "onex-worktree-reconcile.service").read_text() == result.service
    assert [
        "systemctl",
        "--user",
        "enable",
        "--now",
        "onex-worktree-reconcile.timer",
    ] in calls
    assert any("list-timers" in call for call in calls)
    record = json.loads((tmp_path / "state" / "installed.json").read_text())
    assert record["revision"] == result.revision


def test_install_does_not_report_success_after_systemctl_failure(
    tmp_path: Path,
) -> None:
    path = overlay(tmp_path)

    def run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 1, "", "manager refused")

    result = HandlerWorktreeSchedule(
        unit_directory=tmp_path / "units", runner=run
    ).handle(
        ModelWorktreeScheduleRequest(
            mode="install", host="lab-host", overlay_path=str(path)
        )
    )
    assert result.exit_code != 0
    assert "manager refused" in result.stderr
    assert not (tmp_path / "state" / "installed.json").exists()


def test_overlay_uses_typed_reconcile_commands(tmp_path: Path) -> None:
    path = overlay(tmp_path)
    parsed = ModelWorktreeScheduleOverlay.model_validate(
        yaml.safe_load(path.read_text())
    )
    assert parsed.hosts["lab-host"].execute is False
    assert parsed.hosts["lab-host"].policy.quiet_hours == 6


def test_tick_refreshes_correlation_and_writes_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from omnimarket.nodes.node_worktree_reconcile_effect.__main__ import main

    path = overlay(tmp_path)
    monkeypatch.delenv("KAFKA_BROKERS", raising=False)
    assert main(["tick", "--host", "lab-host", "--overlay", str(path)]) == 0
    first = json.loads(capsys.readouterr().out)
    assert main(["tick", "--host", "lab-host", "--overlay", str(path)]) == 0
    second = json.loads(capsys.readouterr().out)
    assert (
        first["completed_event"]["correlation_id"]
        != second["completed_event"]["correlation_id"]
    )
    receipt = json.loads((tmp_path / "state" / "latest.json").read_text())
    assert receipt["completed_event"] == second["completed_event"]
    assert receipt["completed_event"]["scanned"] == 0


def test_tick_lock_prevents_an_overlapping_sweep(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import fcntl

    from omnimarket.nodes.node_worktree_reconcile_effect.__main__ import main

    path = overlay(tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    with (state / "tick.lock").open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert main(["tick", "--host", "lab-host", "--overlay", str(path)]) == 0
    assert "LOCKED" in capsys.readouterr().out
    assert not (state / "latest.json").exists()


def test_install_refuses_uncommitted_project(tmp_path: Path) -> None:
    path = overlay(tmp_path)
    project = tmp_path / "project"
    (project / "new.py").write_text("uncommitted = True\n")
    units = tmp_path / "units"
    result = HandlerWorktreeSchedule(unit_directory=units).handle(
        ModelWorktreeScheduleRequest(
            mode="install", host="lab-host", overlay_path=str(path)
        )
    )
    assert result.exit_code != 0
    assert "clean" in result.stderr
    assert not units.exists()


def test_install_refuses_wrong_expected_revision(tmp_path: Path) -> None:
    path = overlay(tmp_path)
    units = tmp_path / "units"
    result = HandlerWorktreeSchedule(unit_directory=units).handle(
        ModelWorktreeScheduleRequest(
            mode="install",
            host="lab-host",
            overlay_path=str(path),
            expected_revision="0" * 40,
        )
    )
    assert result.exit_code != 0
    assert "revision" in result.stderr
    assert not units.exists()


@pytest.mark.parametrize("bad_host", ['bad"host', "bad\nhost", "$USER", "%h"])
def test_host_is_never_interpreted_as_unit_syntax(
    tmp_path: Path, bad_host: str
) -> None:
    path = overlay(tmp_path)
    result = HandlerWorktreeSchedule(unit_directory=tmp_path / "units").handle(
        ModelWorktreeScheduleRequest(
            mode="render", host=bad_host, overlay_path=str(path)
        )
    )
    assert result.exit_code != 0
    assert "literal host identifier" in result.stderr


@pytest.mark.parametrize("case", ["fresh", "stale", "future", "failed", "other_host"])
def test_status_requires_a_fresh_matching_successful_tick(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: str,
) -> None:
    from datetime import UTC, datetime, timedelta

    from omnimarket.nodes.node_worktree_reconcile_effect.__main__ import main

    path = overlay(tmp_path)

    def run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            argv, 0, "onex-worktree-reconcile.timer\n", ""
        )

    handler = HandlerWorktreeSchedule(unit_directory=tmp_path / "units", runner=run)
    installed = handler.handle(
        ModelWorktreeScheduleRequest(
            mode="install", host="lab-host", overlay_path=str(path)
        )
    )
    assert installed.exit_code == 0
    monkeypatch.delenv("KAFKA_BROKERS", raising=False)
    assert main(["tick", "--host", "lab-host", "--overlay", str(path)]) == 0
    capsys.readouterr()
    receipt_path = tmp_path / "state" / "latest.json"
    receipt = json.loads(receipt_path.read_text())
    if case in ("stale", "future"):
        delta = timedelta(days=-1 if case == "stale" else 1)
        receipt["completed_event"]["finished_at"] = (
            datetime.now(UTC) + delta
        ).isoformat()
    elif case == "failed":
        receipt["completed_event"]["failures"] = 1
    elif case == "other_host":
        receipt["completed_event"]["host"] = "other"
    receipt_path.write_text(json.dumps(receipt))
    result = handler.handle(
        ModelWorktreeScheduleRequest(
            mode="status", host="lab-host", overlay_path=str(path)
        )
    )
    assert (result.exit_code == 0) is (case == "fresh")


def test_schedule_handler_is_bound_by_the_node_contract(tmp_path: Path) -> None:
    """The host schedule must resolve through the node contract like its effect."""
    from importlib import import_module

    contract_path = (
        Path(__file__).resolve().parents[1]
        / "src/omnimarket/nodes/node_worktree_reconcile_effect/contract.yaml"
    )
    contract = yaml.safe_load(contract_path.read_text())
    routes = contract["handler_routing"]["handlers"]
    route = next(item for item in routes if item["operation"] == "schedule")
    handler_type = getattr(
        import_module(route["handler"]["module"]), route["handler"]["name"]
    )
    request_module, _, request_name = route["input_model"].rpartition(".")
    request_type = getattr(import_module(request_module), request_name)
    result = handler_type(unit_directory=tmp_path / "units").handle(
        request_type(
            mode="render", host="lab-host", overlay_path=str(overlay(tmp_path))
        )
    )
    result_module, _, result_name = route["output_model"].rpartition(".")
    assert isinstance(result, getattr(import_module(result_module), result_name))
    assert result.exit_code == 0
    assert "onex-worktree-reconcile.service" in result.timer


def test_host_reconciliation_is_not_a_boot_dispatched_route() -> None:
    """The host CLI supplies dependencies that the runtime cannot inject."""
    contract_path = (
        Path(__file__).resolve().parents[1]
        / "src/omnimarket/nodes/node_worktree_reconcile_effect/contract.yaml"
    )
    contract = yaml.safe_load(contract_path.read_text())
    assert contract["handler"]["class"] == "HandlerWorktreeReconcile"
    assert contract["runtime_dispatch"]["external_trigger"] is True
    routes = contract["handler_routing"]["handlers"]
    assert [route["operation"] for route in routes] == ["schedule"]


def test_the_unit_declares_its_priority_and_the_installer_replaces_a_hand_made_idle_drop_in(
    tmp_path: Path,
) -> None:
    """Recorded case (OMN-22799): a hand-made drop-in set IOSchedulingClass=idle, a git read
    that takes 0.1 s took 5 to 10 s behind lane work, and the 60 s git budget fired."""
    path = overlay(tmp_path)
    units = tmp_path / "units"
    drop_in = units / "onex-worktree-reconcile.service.d" / "10-low-priority.conf"
    drop_in.parent.mkdir(parents=True)
    drop_in.write_text("[Service]\nNice=19\nIOSchedulingClass=idle\nCPUWeight=10\n")

    def run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        output = "active\n" if "is-active" in argv else "enabled\n"
        if "list-timers" in argv:
            output = "onex-worktree-reconcile.timer onex-worktree-reconcile.service\n"
        return subprocess.CompletedProcess(argv, 0, output, "")

    result = HandlerWorktreeSchedule(unit_directory=units, runner=run).handle(
        ModelWorktreeScheduleRequest(
            mode="install", host="lab-host", overlay_path=str(path)
        )
    )
    assert result.exit_code == 0
    assert "IOSchedulingClass=best-effort" in result.dropin
    assert "IOSchedulingPriority=7" in result.dropin
    assert "Nice=19" in result.dropin
    assert "CPUWeight=10" in result.dropin
    assert drop_in.read_text() == result.dropin
    assert "idle" not in drop_in.read_text()
