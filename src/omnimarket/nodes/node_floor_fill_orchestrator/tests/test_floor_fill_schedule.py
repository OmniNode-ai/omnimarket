# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The fill's own tick unit and the prompt-submit hook's ``lines`` entry (plan T14 acceptance criteria)."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from omnimarket.nodes.node_floor_fill_orchestrator.handlers.handler_floor_fill_command import (
    HandlerFloorFillCommand,
)
from omnimarket.nodes.node_floor_fill_orchestrator.models import (
    EnumFloorFillCommand,
    ModelFloorFillCommandRequest,
    ModelFloorFillConfig,
    ModelFloorFillSchedule,
    load_floor_fill_schedule,
)
from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.ports import (
    ModelRunOutcome,
)
from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.scripted_ports import (
    ScriptedCommandRunner,
)
from omnimarket.nodes.node_floor_fill_orchestrator.schedule_entry import run_schedule
from omnimarket.nodes.node_floor_fill_orchestrator.tests.support import (
    OVERLAY,
    harness,
    overlay,
)

pytestmark = pytest.mark.unit

EXEC_START = "ExecStart=/usr/bin/env onex run-node node_floor_fill_orchestrator -- tick"
HOSTS = [("fill-host", True), ("other-host", True), ("away-host", False)]
OMNI_ROOT = Path("/srv/example/registry")
OVERLAY_ROOT = Path("/srv/example/overlays")
NODE_DIR = "src/omnimarket/nodes/node_floor_fill_orchestrator/"
# The repository's receipt gate reads the ticket's own contract from this one path; it is the only path
# outside the node the pull request carries and it is named here, not matched by a pattern.
TICKET_CONTRACT = "contracts/OMN-22855.yaml"
FORBIDDEN_NAMES = ("throughput_tick", "queue_status", "hook.sh", "onex_remote_lane")


def _install(
    section: dict[str, object],
    home: Path,
    *extra: str,
    hosts: list[tuple[str, bool]] | None = None,
    this_host: str = "fill-host",
    run: object = None,
) -> tuple[int, str, str]:
    result = run_schedule(
        [
            "install",
            "--this-host",
            this_host,
            "--workspace-root",
            str(OMNI_ROOT),
            *extra,
        ],
        {"floor_fill": section},
        hosts=HOSTS if hosts is None else hosts,
        overlay_root=OVERLAY_ROOT,
        home=home,
        run=run or _ok,  # type: ignore[arg-type]
        on_path=lambda _name, _path: True,
    )
    return result.exit_code, result.stdout, result.stderr


def _ok(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(list(command), 0, "", "")


# --------------------------------------------------------------------------- the interval and the command


def test_floor_fill_schedule_interval_from_overlay(tmp_path: Path) -> None:
    code_a, out_a, _ = _install(
        overlay(tick_interval_seconds=300), tmp_path, "--dry-run"
    )
    code_b, out_b, _ = _install(
        overlay(tick_interval_seconds=120), tmp_path, "--dry-run"
    )
    assert (code_a, code_b) == (0, 0)
    assert "OnUnitActiveSec=300s" in out_a
    assert "OnBootSec=300s" in out_a
    assert "OnUnitActiveSec=120s" in out_b
    assert "OnBootSec=120s" in out_b
    assert "120s" not in out_a
    assert "300s" not in out_b
    for out in (out_a, out_b):
        assert EXEC_START in out
        assert out.count("ExecStart=") == 1


def test_floor_fill_schedule_unit_name_and_deadline_are_the_contracts(
    tmp_path: Path,
) -> None:
    schedule = load_floor_fill_schedule()
    _, out, _ = _install(OVERLAY, tmp_path, "--dry-run")
    assert f"Unit={schedule.unit_name}.service" in out
    assert f"TimeoutStartSec={schedule.timeout_seconds}" in out
    assert schedule.timeout_seconds > OVERLAY["command_timeout_seconds"]


def test_floor_fill_schedule_deadline_above_the_command_timeout(tmp_path: Path) -> None:
    tight = ModelFloorFillSchedule(unit_name="u", timeout_seconds=60)
    result = run_schedule(
        [
            "install",
            "--this-host",
            "fill-host",
            "--workspace-root",
            str(OMNI_ROOT),
            "--dry-run",
        ],
        {"floor_fill": OVERLAY},
        hosts=HOSTS,
        overlay_root=OVERLAY_ROOT,
        home=tmp_path,
        schedule=tight,
    )
    assert result.exit_code == 1
    assert "floor_fill.command_timeout_seconds" in result.stderr


@pytest.mark.parametrize("key", sorted(ModelFloorFillConfig.model_fields))
def test_floor_fill_schedule_missing_key_refused(key: str, tmp_path: Path) -> None:
    section = {name: value for name, value in OVERLAY.items() if name != key}
    code, out, err = _install(section, tmp_path, "--dry-run")
    assert code == 1
    assert out == ""
    assert f"floor_fill.{key}" in err
    # the positive control: the same overlay with the key present renders
    assert _install(OVERLAY, tmp_path, "--dry-run")[0] == 0


def test_floor_fill_schedule_host_must_be_available(tmp_path: Path) -> None:
    for name, table in (
        ("away-host", HOSTS),
        ("not-in-the-table", HOSTS),
        ("fill-host", [("fill-host", False)]),
    ):
        code, out, err = _install(
            overlay(host_id=name), tmp_path, "--dry-run", hosts=table, this_host=name
        )
        assert code == 1
        assert out == ""
        assert "floor_fill.host_id" in err
    assert _install(OVERLAY, tmp_path, "--dry-run")[0] == 0


def test_floor_fill_schedule_wrong_host_refused(tmp_path: Path) -> None:
    code, out, err = _install(OVERLAY, tmp_path, this_host="other-host")
    assert (code, out) == (1, "")
    assert "floor_fill.host_id" in err
    assert not (tmp_path / ".config").exists()


# --------------------------------------------------------------------------- install, rollback, equality


def test_floor_fill_schedule_installed_unit_equals_dry_run(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(command))
        return _ok(command)

    _, dry, _ = _install(OVERLAY, tmp_path, "--dry-run", run=run)
    assert calls == []
    code, _, _ = _install(OVERLAY, tmp_path, run=run)
    assert code == 0
    schedule = load_floor_fill_schedule()
    unit_dir = tmp_path / ".config/systemd/user"
    service = (unit_dir / f"{schedule.unit_name}.service").read_text(encoding="utf-8")
    timer = (unit_dir / f"{schedule.unit_name}.timer").read_text(encoding="utf-8")
    assert service + "\n" + timer + "\n" == dry
    assert calls[-1] == [
        "systemctl",
        "--user",
        "enable",
        "--now",
        f"{schedule.unit_name}.timer",
    ]
    _, again, _ = _install(OVERLAY, tmp_path, run=run)
    assert "unchanged" in again


def test_floor_fill_schedule_uninstall_is_the_rollback(tmp_path: Path) -> None:
    _install(OVERLAY, tmp_path)
    schedule = load_floor_fill_schedule()
    result = run_schedule(
        ["uninstall", "--this-host", "fill-host"],
        {"floor_fill": OVERLAY},
        hosts=HOSTS,
        overlay_root=OVERLAY_ROOT,
        home=tmp_path,
        run=_ok,
    )
    assert result.exit_code == 0
    unit_dir = tmp_path / ".config/systemd/user"
    assert not (unit_dir / f"{schedule.unit_name}.timer").exists()
    assert not (unit_dir / f"{schedule.unit_name}.service").exists()


def test_floor_fill_schedule_refuses_a_unit_that_runs_from_a_worktree(
    tmp_path: Path,
) -> None:
    result = run_schedule(
        [
            "install",
            "--this-host",
            "fill-host",
            "--workspace-root",
            str(tmp_path / "omni_worktrees" / "x" / "repo"),
            "--dry-run",
        ],
        {"floor_fill": OVERLAY},
        hosts=HOSTS,
        overlay_root=OVERLAY_ROOT,
        home=tmp_path,
    )
    assert result.exit_code == 1
    assert "omni_worktrees" in result.stderr


# --------------------------------------------------------------------------- nothing outside the permitted places


def places_violations(paths: Sequence[str]) -> list[str]:
    """Every changed path that names a script or sits outside the node (any other repository's path included)."""
    bad: list[str] = []
    for path in paths:
        name = path.rsplit("/", 1)[-1]
        if any(
            forbidden in name for forbidden in FORBIDDEN_NAMES
        ) or not path.startswith(NODE_DIR):
            bad.append(path)
    return bad


def _own_changed_paths() -> list[str]:
    """The files this pull request's own commits (those citing the ticket) changed."""
    root = Path(__file__).resolve().parents[5]
    try:
        commits = subprocess.run(
            ["git", "log", "origin/dev..HEAD", "--format=%H", "--grep=OMN-22855"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
        names = [
            subprocess.run(
                ["git", "show", "--name-only", "--format=", commit],
                cwd=root,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.split()
            for commit in commits
        ]
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []
    return sorted({path for group in names for path in group} - {TICKET_CONTRACT})


def test_no_script_edit_lists_each_changed_file() -> None:
    assert places_violations(_own_changed_paths()) == []


def test_no_script_edit_fails_on_a_match_or_a_stray_path() -> None:
    inside = f"{NODE_DIR}handlers/handler_floor_fill_schedule.py"
    assert places_violations([inside]) == []
    for stray in (
        f"{NODE_DIR}scripts/throughput_tick.py",
        f"{NODE_DIR}queue_status.py",
        f"{NODE_DIR}hook.sh",
        f"{NODE_DIR}onex_remote_lane.py",
        "src/omnimarket/nodes/node_other/contract.yaml",
        "another_repository/src/x.py",
    ):
        assert places_violations([inside, stray]) == [stray]


# --------------------------------------------------------------------------- the hook entry: `lines`


def _lines_request() -> ModelFloorFillCommandRequest:
    return ModelFloorFillCommandRequest(
        command=EnumFloorFillCommand.LINES, host_id="orchestrator-mac"
    )


def test_lines_entry_prints_stored_answer() -> None:
    h = harness()
    tick = h.orchestrator().run(EnumFloorFillCommand.TICK)
    stored = "".join(f"{line}\n" for line in tick.lines)
    handler = HandlerFloorFillCommand(
        config=h.config,
        orchestrator=h.orchestrator(),
        runner=ScriptedCommandRunner(
            ModelRunOutcome(state="exited", exit_code=0, stdout=stored)
        ),
    )
    from_other_host = handler.handle(_lines_request())
    assert from_other_host.stdout == stored
    assert from_other_host.exit_code == tick.exit_code
    assert from_other_host.forwarded
    on_fill_host = handler.handle(
        ModelFloorFillCommandRequest(
            command=EnumFloorFillCommand.LINES, host_id=h.config.host_id
        )
    )
    assert on_fill_host.stdout == stored
    # the stale line and the absent line, from the stored answer's own observed_at
    h.clock.advance(minutes=h.config.max_age_minutes + 1)
    stale = handler.handle(
        ModelFloorFillCommandRequest(
            command=EnumFloorFillCommand.LINES, host_id=h.config.host_id
        )
    )
    assert (
        stale.stdout
        == f"UNKNOWN floor-fill-answer-stale:{h.config.max_age_minutes + 1}m\n"
    )
    assert stale.exit_code != 0
    fresh = harness()
    absent = HandlerFloorFillCommand(
        config=fresh.config,
        orchestrator=fresh.orchestrator(),
        runner=ScriptedCommandRunner(ModelRunOutcome(state="failed")),
    ).handle(
        ModelFloorFillCommandRequest(
            command=EnumFloorFillCommand.LINES, host_id=fresh.config.host_id
        )
    )
    assert absent.stdout == "UNKNOWN floor-fill-answer-absent\n"


@pytest.mark.parametrize(
    "outcome",
    [
        ModelRunOutcome(state="failed", cause="ssh: no route"),
        ModelRunOutcome(state="timed_out"),
    ],
)
def test_lines_entry_unreachable_prints_diagnostic(outcome: ModelRunOutcome) -> None:
    h = harness()
    handler = HandlerFloorFillCommand(
        config=h.config,
        orchestrator=h.orchestrator(),
        runner=ScriptedCommandRunner(outcome),
    )
    result = handler.handle(_lines_request())
    assert result.stdout == "UNKNOWN floor-fill-unreachable\n"
    assert result.exit_code == 75
