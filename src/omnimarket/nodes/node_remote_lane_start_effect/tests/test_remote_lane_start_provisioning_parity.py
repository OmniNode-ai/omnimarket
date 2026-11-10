# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Parity of the ssh transport's first provisioning steps with the frozen runner script (OMN-23425).

Each step sends the host the same commands and prints the same lines as the runner did for the recorded
lane start (``recorded_runner_steps``): the registry root resolution and the run directories. The
remaining provisioning steps are still answered "not run by the ssh transport" (see the last test).
"""

from __future__ import annotations

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
        self, argv: Sequence[str], timeout_seconds: float
    ) -> ModelHostCommandResult:
        self.argvs.append(list(argv))
        return self.answer


def step(host: ModelLaneHostRef = REMOTE) -> ModelLaneHostStepRequest:
    return ModelLaneHostStepRequest(
        host=host, lane="lane-x", run_id="rlane-example-000001", timeout_seconds=30
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


def test_the_other_provisioning_steps_still_answer_not_run_by_the_ssh_transport() -> (
    None
):
    answered = transport(Recorder(ModelHostCommandResult(exit_code=0))).mirror_ledger(
        step()
    )
    assert answered.ok is False
    assert "not run by the ssh transport" in (answered.error or "")
