# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The fill's own tick unit and the prompt-submit hook's ``lines`` entry (plan T14 acceptance criteria)."""

from __future__ import annotations

import json
import shlex
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from omnimarket.nodes.node_floor_fill_orchestrator.handlers.adapter_floor_fill_schedule import (
    NODE_MODULE,
    Runner,
)
from omnimarket.nodes.node_floor_fill_orchestrator.handlers.handler_floor_fill_command import (
    HandlerFloorFillCommand,
)
from omnimarket.nodes.node_floor_fill_orchestrator.models import (
    EXIT_CODE_BY_STATUS,
    EnumFloorFillCommand,
    EnumFloorFillStatus,
    ModelFloorFillCommandRequest,
    ModelFloorFillConfig,
    ModelFloorFillSchedule,
    load_floor_fill_schedule,
)
from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.ports import (
    ModelRunOutcome,
)
from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.scripted_ports import (
    ScriptedCommandForward,
)
from omnimarket.nodes.node_floor_fill_orchestrator.runtime_floor_fill_orchestrator import (
    main as runtime_main,
)
from omnimarket.nodes.node_floor_fill_orchestrator.runtime_floor_fill_schedule import (
    run_schedule,
)
from omnimarket.nodes.node_floor_fill_orchestrator.tests.support import (
    OVERLAY,
    harness,
    overlay,
)

pytestmark = pytest.mark.unit

VENV_PYTHON = f"{'/srv/example/registry'}/.onex-dispatch-venv/bin/python"
HOSTS = [("fill-host", True), ("other-host", True), ("away-host", False)]
OMNI_ROOT = Path("/srv/example/registry")
NODE_DIR = "src/omnimarket/nodes/node_floor_fill_orchestrator/"
# Names no file of the node may carry: the fill is a node, never a script, a hook or a copy of another lane's tool.
FORBIDDEN_NAMES = ("throughput_tick", "queue_status", "hook.sh", "onex_remote_lane")


def _install(
    section: dict[str, object],
    home: Path,
    *extra: str,
    hosts: list[tuple[str, bool]] | None = None,
    this_host: str = "fill-host",
    run: Runner | None = None,
    omni_root: Path = OMNI_ROOT,
) -> tuple[int, str, str]:
    result = run_schedule(
        [
            "install",
            "--this-host",
            this_host,
            "--workspace-root",
            str(omni_root),
            *extra,
        ],
        {"floor_fill": section},
        hosts=HOSTS if hosts is None else hosts,
        home=home,
        run=run or _ok,
    )
    return result.exit_code, result.stdout, result.stderr


def _registry(tmp_path: Path) -> Path:
    """A registry root whose dispatch venv holds an interpreter file, so a real install passes its check."""
    root = tmp_path / "registry"
    python = root / ".onex-dispatch-venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    return root


def exec_words(rendered: str) -> list[str]:
    """The words of the unit's one ExecStart line, as systemd reads them (``%%`` and ``$$`` unescaped)."""
    lines = [line for line in rendered.splitlines() if line.startswith("ExecStart=")]
    assert len(lines) == 1
    text = lines[0].removeprefix("ExecStart=").replace("%%", "%").replace("$$", "$")
    return shlex.split(text)


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
        assert out.count("ExecStart=") == 1
        assert exec_words(out)[-1] == "tick"


def test_floor_fill_schedule_unit_name_and_deadline_are_the_contracts(
    tmp_path: Path,
) -> None:
    schedule = load_floor_fill_schedule()
    _, out, _ = _install(OVERLAY, tmp_path, "--dry-run")
    assert f"Unit={schedule.unit_name}.service" in out
    assert f"TimeoutStartSec={schedule.timeout_seconds}" in out
    assert schedule.timeout_seconds > OVERLAY["command_timeout_seconds"]


def test_floor_fill_schedule_exec_is_the_runtime_module_form(tmp_path: Path) -> None:
    """AC1: the unit runs the dispatch venv's python on the runtime module with the overlay as JSON and the host."""
    _, out, _ = _install(OVERLAY, tmp_path, "--dry-run")
    words = exec_words(out)
    assert words[:3] == [VENV_PYTHON, "-m", NODE_MODULE]
    assert words[3] == "--overlay-json"
    assert json.loads(words[4]) == {
        "floor_fill": OVERLAY,
        "lab_hosts": [{"name": OVERLAY["host_id"], "available": True}],
    }
    assert words[5:] == ["--this-host", OVERLAY["host_id"], "tick"]
    assert "run-node" not in out
    assert "/usr/bin/env" not in out


def test_floor_fill_schedule_exec_start_runs_the_runtime_module(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The rendered words are accepted by the runtime module itself (its argv after ``-m <module>``)."""
    _, out, _ = _install(OVERLAY, tmp_path, "--dry-run")
    arguments = exec_words(out)[3:]
    code = runtime_main([*arguments[:-1], "lines"])
    printed = capsys.readouterr().out
    assert printed.startswith("UNKNOWN floor-fill-answer-absent")
    assert runtime_main([*arguments[:-1], "lines"]) == code


@pytest.mark.parametrize("bad", ["", "not json", "[1]", "null"])
def test_floor_fill_runtime_unreadable_overlay_json_is_a_loud_unknown(
    bad: str, capsys: pytest.CaptureFixture[str]
) -> None:
    code = runtime_main(["--overlay-json", bad, "--this-host", "fill-host", "lines"])
    assert code == EXIT_CODE_BY_STATUS[EnumFloorFillStatus.FAILED]
    assert capsys.readouterr().out == "UNKNOWN fill-state:overlay:unreadable\n"


def test_floor_fill_schedule_install_refused_without_the_dispatch_venv(
    tmp_path: Path,
) -> None:
    code, _, err = _install(OVERLAY, tmp_path)
    assert code == 1
    assert "no dispatch venv interpreter at" in err
    assert not (tmp_path / ".config/systemd/user").exists()


def test_floor_fill_schedule_install_refused_when_the_module_does_not_import(
    tmp_path: Path,
) -> None:
    def run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        if command[1:2] == ["-c"]:
            return subprocess.CompletedProcess(list(command), 1, "", "No module")
        return _ok(command)

    code, _, err = _install(OVERLAY, tmp_path, run=run, omni_root=_registry(tmp_path))
    assert code == 1
    assert NODE_MODULE in err
    assert not (tmp_path / ".config/systemd/user").exists()


def test_floor_fill_schedule_deadline_above_the_command_timeout(tmp_path: Path) -> None:
    tight = ModelFloorFillSchedule(
        unit_name="u", timeout_seconds=60, dispatch_venv=".onex-dispatch-venv"
    )
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

    root = _registry(tmp_path)
    _, dry, _ = _install(OVERLAY, tmp_path, "--dry-run", run=run, omni_root=root)
    assert calls == []
    code, _, _ = _install(OVERLAY, tmp_path, run=run, omni_root=root)
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
    _, again, _ = _install(OVERLAY, tmp_path, run=run, omni_root=root)
    assert "unchanged" in again


def test_floor_fill_schedule_uninstall_is_the_rollback(tmp_path: Path) -> None:
    _install(OVERLAY, tmp_path, omni_root=_registry(tmp_path))
    schedule = load_floor_fill_schedule()
    unit_dir = tmp_path / ".config/systemd/user"
    assert (unit_dir / f"{schedule.unit_name}.timer").exists()
    result = run_schedule(
        ["uninstall", "--this-host", "fill-host"],
        {"floor_fill": OVERLAY},
        hosts=HOSTS,
        home=tmp_path,
        run=_ok,
    )
    assert result.exit_code == 0
    assert not (unit_dir / f"{schedule.unit_name}.timer").exists()
    assert not (unit_dir / f"{schedule.unit_name}.service").exists()


def test_floor_fill_schedule_uninstall_reports_a_failed_disable(
    tmp_path: Path,
) -> None:
    _install(OVERLAY, tmp_path, omni_root=_registry(tmp_path))

    def run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        if "disable" in command:
            return subprocess.CompletedProcess(
                list(command), 5, "", "Failed to disable"
            )
        return _ok(command)

    result = run_schedule(
        ["uninstall", "--this-host", "fill-host"],
        {"floor_fill": OVERLAY},
        hosts=HOSTS,
        home=tmp_path,
        run=run,
    )
    assert result.exit_code != 0
    assert "Failed to disable" in result.stderr


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


def _node_files(root: Path) -> list[str]:
    """Every file below the node's directory, as a repository-relative path.

    Read from the working tree, never from git history, so the check runs the same on a runner that has no
    ``origin/dev`` range and is never skipped.
    """
    return sorted(
        path.relative_to(root).as_posix()
        for path in (root / NODE_DIR).rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    )


def test_no_script_edit_lists_each_node_file() -> None:
    root = Path(__file__).resolve().parents[5]
    files = _node_files(root)
    assert f"{NODE_DIR}handlers/adapter_floor_fill_schedule.py" in files
    assert places_violations(files) == []


def test_no_script_edit_reads_a_forbidden_file_in_the_node_tree(
    tmp_path: Path,
) -> None:
    node = tmp_path / NODE_DIR
    (node / "handlers").mkdir(parents=True)
    (node / "handlers" / "adapter.py").write_text("x = 1\n", encoding="utf-8")
    assert places_violations(_node_files(tmp_path)) == []
    (node / "throughput_tick.py").write_text("x = 1\n", encoding="utf-8")
    assert places_violations(_node_files(tmp_path)) == [f"{NODE_DIR}throughput_tick.py"]


def test_no_script_edit_fails_on_a_match_or_a_stray_path() -> None:
    inside = f"{NODE_DIR}handlers/adapter_floor_fill_schedule.py"
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
        runner=ScriptedCommandForward(
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
        runner=ScriptedCommandForward(
            ModelRunOutcome(state="failed", cause="forward-unused")
        ),
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
        runner=ScriptedCommandForward(outcome),
    )
    result = handler.handle(_lines_request())
    assert result.stdout == "UNKNOWN floor-fill-unreachable\n"
    assert result.exit_code == 75
