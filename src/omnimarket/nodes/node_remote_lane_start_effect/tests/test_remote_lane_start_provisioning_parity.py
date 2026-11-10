# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Parity of the ssh transport's first provisioning steps with the frozen runner script (OMN-23425).

Each step sends the host the same commands and prints the same lines as the runner did for the recorded
lane start (``recorded_runner_steps``): the registry root resolution, the run directories, the queued
ledger rows, the lane's changes and the run-directory hold. The remaining provisioning steps are still
answered "not run by the ssh transport" (see the last test).
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest
from omnibase_spi.contracts.services import ModelLaneHostRef, ModelLaneHostStepRequest

from omnimarket.nodes.node_remote_lane_start_effect.adapters.adapter_ssh_transport import (
    ModelHostCommandResult,
    ProtocolHostCommandRunner,
    SshLaneHostTransport,
)
from omnimarket.nodes.node_remote_lane_start_effect.tests.recorded_runner_steps import (
    RECORDED_CHANGES_SCRIPT,
    RECORDED_HOLD_GUARD_SCRIPT,
    RECORDED_LEDGER_OUTBOX_SCRIPT,
    RECORDED_OMNI_HOME_SCRIPT,
    RECORDED_SCRATCH_SCRIPT,
)

pytestmark = pytest.mark.unit

REMOTE = ModelLaneHostRef(name="h-test", target="h-test.example.invalid")
LOCAL = ModelLaneHostRef(name="h-local", local=True)


class Recorder:
    def __init__(self, answer: ModelHostCommandResult) -> None:
        self.answer = answer
        self.argvs: list[list[str]] = []

    def run(
        self,
        argv: Sequence[str],
        timeout_seconds: float,
        stdin: bytes | None = None,
    ) -> ModelHostCommandResult:
        self.argvs.append(list(argv))
        return self.answer


def step(
    host: ModelLaneHostRef = REMOTE,
    lane: str = "lane-x",
    registry_root: str | None = None,
) -> ModelLaneHostStepRequest:
    return ModelLaneHostStepRequest(
        host=host,
        lane=lane,
        run_id="rlane-example-000001",
        registry_root=registry_root,
        timeout_seconds=30,
    )


def as_recorded(script: str) -> str:
    """The runner's recorded text with its home-relative run root swapped for the declared root."""
    return (
        script.replace('LR="$HOME/lab-run"; ', "")
        .replace("$HOME/lab-run/runs", "/srv/example/runs")
        .replace("$LR/runs", "/srv/example/runs")
    )


def transport(
    runner: ProtocolHostCommandRunner | None, root: str = "/srv/example/runs"
) -> SshLaneHostTransport:
    return SshLaneHostTransport(
        ssh_argv=["ssh"],
        host_run_root=root,
        call_timeout_seconds=30,
        detach_argv=["setsid"],
        registry_root_candidates=["/srv/example/registry"],
        registry_marker="omnibase_infra",
        runner=runner,
    )


def test_resolve_registry_root_sends_the_recorded_runner_script() -> None:
    runner = Recorder(
        ModelHostCommandResult(
            exit_code=0, stdout="REMOTE-OMNI-HOME /srv/example/registry\n"
        )
    )
    answered = transport(runner).resolve_registry_root(step())
    assert runner.argvs == [
        ["ssh", "h-test.example.invalid", RECORDED_OMNI_HOME_SCRIPT]
    ]
    assert answered.registry_root == "/srv/example/registry"


def test_resolve_registry_root_prints_the_runner_refusal_line_and_exit(
    tmp_path: Path,
) -> None:
    none_here = SshLaneHostTransport(
        ssh_argv=["ssh"],
        host_run_root=str(tmp_path / "runs"),
        call_timeout_seconds=30,
        detach_argv=["setsid"],
        registry_root_candidates=[str(tmp_path / "no-registry")],
        registry_marker="omnibase_infra",
    )
    answered = none_here.resolve_registry_root(step(LOCAL))
    assert answered.registry_root is None
    assert answered.error is not None
    assert answered.error.startswith("REMOTE-OMNI-HOME-NONE ")
    assert "holds omnibase_infra" in answered.error


def test_resolve_registry_root_finds_a_root_that_holds_the_marker(
    tmp_path: Path,
) -> None:
    (tmp_path / "registry" / "omnibase_infra").mkdir(parents=True)
    found = SshLaneHostTransport(
        ssh_argv=["ssh"],
        host_run_root=str(tmp_path / "runs"),
        call_timeout_seconds=30,
        detach_argv=["setsid"],
        registry_root_candidates=[str(tmp_path / "bare"), str(tmp_path / "registry")],
        registry_marker="omnibase_infra",
    ).resolve_registry_root(step(LOCAL))
    assert found.registry_root == str(tmp_path / "registry")


def test_prepare_run_dirs_sends_the_recorded_runner_lines_for_the_declared_root() -> (
    None
):
    runner = Recorder(
        ModelHostCommandResult(exit_code=0, stdout="REMOTE-HOME /home/x\n")
    )
    answered = transport(runner, root="/srv/example/runs").prepare_run_dirs(step())
    sent = runner.argvs[0][-1]
    recorded = RECORDED_SCRATCH_SCRIPT.replace('LR="$HOME/lab-run"; ', "").replace(
        "$LR/runs", "/srv/example/runs"
    )
    assert sent == recorded
    assert answered.run_dir == "/srv/example/runs/rlane-example-000001"
    assert answered.context_dir == "/srv/example/runs/rlane-example-000001.ctx"


def test_prepare_run_dirs_makes_both_directories(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    answered = transport(None, root=str(root)).prepare_run_dirs(step(LOCAL))
    assert answered.error is None
    assert (root / "rlane-example-000001").is_dir()
    assert (root / "rlane-example-000001.ctx").is_dir()


def test_prepare_run_dirs_names_the_refusal_when_the_root_cannot_be_made() -> None:
    blocked = transport(None, root="/proc/no-such/runs").prepare_run_dirs(step(LOCAL))
    assert blocked.run_dir is None
    assert blocked.error


def test_read_ledger_outbox_sends_the_recorded_runner_line_and_returns_the_queued_rows() -> (
    None
):
    runner = Recorder(
        ModelHostCommandResult(exit_code=0, stdout="row one\n\nrow two\n")
    )
    answered = transport(runner).read_ledger_outbox(step())
    assert runner.argvs == [
        ["ssh", "h-test.example.invalid", as_recorded(RECORDED_LEDGER_OUTBOX_SCRIPT)]
    ]
    assert answered.rows == ("row one", "row two")
    assert answered.error is None


def test_read_ledger_outbox_answers_no_rows_when_the_lane_queued_none(
    tmp_path: Path,
) -> None:
    answered = transport(None, root=str(tmp_path)).read_ledger_outbox(step(LOCAL))
    assert answered.rows == ()
    assert answered.error is None


def test_read_ledger_outbox_reads_the_file_the_lane_queued(tmp_path: Path) -> None:
    ctx = tmp_path / "rlane-example-000001.ctx"
    ctx.mkdir()
    (ctx / "ledger-outbox.rows").write_text("a | MSG | x\nb | TERMINAL | y\n")
    answered = transport(None, root=str(tmp_path)).read_ledger_outbox(step(LOCAL))
    assert answered.rows == ("a | MSG | x", "b | TERMINAL | y")


def test_read_ledger_outbox_names_a_host_command_that_did_not_run() -> None:
    answered = transport(
        Recorder(ModelHostCommandResult(exit_code=255, stderr="ssh: refused"))
    ).read_ledger_outbox(step())
    assert answered.rows == ()
    assert answered.error == "ssh: refused"


def test_read_lane_changes_sends_the_recorded_runner_script() -> None:
    runner = Recorder(
        ModelHostCommandResult(
            exit_code=0,
            stdout=(
                "LANE-CHANGES tree=/t/a file=src/b.py\n"
                "noise\n"
                "LANE-CHANGES tree=/t/a file=src/a.py\n"
                "LANE-CHANGES tree=/t/c file=src/a.py\n"
            ),
        )
    )
    answered = transport(runner).read_lane_changes(
        step(lane="lane-example", registry_root="/srv/example/registry")
    )
    assert runner.argvs == [["ssh", "h-test.example.invalid", RECORDED_CHANGES_SCRIPT]]
    assert answered.changes == ("src/a.py", "src/b.py")


def test_read_lane_changes_runs_on_a_real_tree_and_names_its_changed_files(
    tmp_path: Path,
) -> None:
    tree = tmp_path / "registry" / "omni_worktrees" / "lane-x" / "repo"
    tree.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(tree)], check=True)
    (tree / "new.txt").write_text("edit")
    answered = transport(None, root=str(tmp_path / "runs")).read_lane_changes(
        step(LOCAL, registry_root=str(tmp_path / "registry"))
    )
    assert answered.error is None
    assert answered.changes == ("new.txt",)


def test_read_lane_changes_without_a_resolved_registry_root_runs_nothing() -> None:
    runner = Recorder(ModelHostCommandResult(exit_code=0))
    answered = transport(runner).read_lane_changes(step())
    assert runner.argvs == []
    assert answered.error == "read_lane_changes: registry root not resolved"


def test_hold_run_dir_guard_sends_the_recorded_runner_script() -> None:
    runner = Recorder(
        ModelHostCommandResult(exit_code=0, stdout="LANE-RETENTION-SAFE\n")
    )
    answered = transport(runner).hold_run_dir_guard(
        step(lane="lane-example", registry_root="/srv/example/registry")
    )
    assert runner.argvs == [
        ["ssh", "h-test.example.invalid", as_recorded(RECORDED_HOLD_GUARD_SCRIPT)]
    ]
    assert answered.ok is True


def test_hold_run_dir_guard_prints_the_runner_kept_line_for_a_git_dir_inside_the_run_directory(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "registry"
    tree = registry / "omni_worktrees" / "lane-x" / "repo"
    tree.mkdir(parents=True)
    git_dir = tmp_path / "runs" / "rlane-example-000001" / "gits" / "repo"
    git_dir.mkdir(parents=True)
    (tree / ".git").write_text(f"gitdir: {git_dir}\n")
    answered = transport(None, root=str(tmp_path / "runs")).hold_run_dir_guard(
        step(LOCAL, registry_root=str(registry))
    )
    assert answered.ok is False
    assert answered.error is not None
    assert answered.error.startswith("LANE-WORKDIR kept tree=")
    assert "reason=hosts-git-dir-of:" in answered.error


def test_hold_run_dir_guard_is_safe_when_no_lane_tree_keeps_its_git_dir_in_the_run_directory(
    tmp_path: Path,
) -> None:
    (tmp_path / "registry").mkdir()
    answered = transport(None, root=str(tmp_path / "runs")).hold_run_dir_guard(
        step(LOCAL, registry_root=str(tmp_path / "registry"))
    )
    assert answered.ok is True


def test_hold_run_dir_guard_without_a_resolved_registry_root_runs_nothing() -> None:
    runner = Recorder(
        ModelHostCommandResult(exit_code=0, stdout="LANE-RETENTION-SAFE\n")
    )
    answered = transport(runner).hold_run_dir_guard(step())
    assert runner.argvs == []
    assert answered.ok is False
    assert answered.error == "hold_run_dir_guard: registry root not resolved"


def test_read_ledger_outbox_sends_the_recorded_runner_script() -> None:
    runner = Recorder(ModelHostCommandResult(exit_code=0, stdout="a\nb\n\nc\n"))
    answered = transport(runner).read_ledger_outbox(step())
    sent = runner.argvs[0][-1]
    recorded = RECORDED_LEDGER_OUTBOX_SCRIPT.replace(
        "$HOME/lab-run/runs", "/srv/example/runs"
    )
    assert sent == recorded
    assert answered.rows == ("a", "b", "c")
    assert answered.error is None


def test_read_ledger_outbox_maps_the_rows_and_error() -> None:
    answered = transport(
        Recorder(ModelHostCommandResult(exit_code=0, stdout="x\n\ny\n"))
    ).read_ledger_outbox(step())
    assert answered.rows == ("x", "y")
    assert answered.error is None
    failed = transport(
        Recorder(ModelHostCommandResult(exit_code=1, stderr="boom"))
    ).read_ledger_outbox(step())
    assert failed.rows == ()
    assert failed.error == "boom"


def test_read_lane_changes_maps_the_files_and_error() -> None:
    answered = transport(
        Recorder(
            ModelHostCommandResult(
                exit_code=0,
                stdout="LANE-CHANGES tree=/t file=b.py\nLANE-CHANGES tree=/t file=a.py\nLANE-CHANGES tree=/t file=b.py\n",
            )
        )
    ).read_lane_changes(step(registry_root="/srv/example/registry"))
    assert answered.changes == ("a.py", "b.py")
    assert answered.error is None
    failed = transport(
        Recorder(ModelHostCommandResult(exit_code=1, stderr="nope"))
    ).read_lane_changes(step(registry_root="/srv/example/registry"))
    assert failed.changes == ()
    assert failed.error == "nope"


def test_hold_run_dir_guard_maps_the_result() -> None:
    ok = transport(
        Recorder(ModelHostCommandResult(exit_code=0, stdout="LANE-RETENTION-SAFE\n"))
    ).hold_run_dir_guard(step(registry_root="/srv/example/registry"))
    assert ok.ok is True
    kept = transport(
        Recorder(
            ModelHostCommandResult(
                exit_code=0, stdout="LANE-WORKDIR kept tree=/w reason=x\n"
            )
        )
    ).hold_run_dir_guard(step(registry_root="/srv/example/registry"))
    assert kept.ok is False
    assert kept.error == "LANE-WORKDIR kept tree=/w reason=x"
    failed = transport(
        Recorder(ModelHostCommandResult(exit_code=1, stderr="err"))
    ).hold_run_dir_guard(step(registry_root="/srv/example/registry"))
    assert failed.ok is False
    assert failed.error == "err"


def test_the_other_provisioning_steps_still_answer_not_run_by_the_ssh_transport() -> (
    None
):
    answered = transport(Recorder(ModelHostCommandResult(exit_code=0))).mirror_ledger(
        step()
    )
    assert answered.ok is False
    assert "not run by the ssh transport" in (answered.error or "")
