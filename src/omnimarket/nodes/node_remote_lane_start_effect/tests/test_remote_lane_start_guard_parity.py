# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Parity of the ssh transport's two lane-guard steps with the frozen runner script (OMN-23425).

``stop_lane_guard`` and ``drain_guard_spool`` send the host the text the runner's ``lane_guard.reap_script`` and
``drain_script`` built for the recorded lane (``recorded_runner_steps``) and read back the same printed
``LANE-GUARD-STOP`` / ``LANE-GUARD-SPOOL`` lines. A change to the transport's script or to the recorded text
fails these.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence

import pytest
from omnibase_spi.contracts.services import ModelLaneHostRef, ModelLaneHostStepRequest

from omnimarket.nodes.node_remote_lane_start_effect.adapters.adapter_ssh_transport import (
    ModelHostCommandResult,
    ProtocolHostCommandRunner,
    SshLaneHostTransport,
    guard_unit_name,
)
from omnimarket.nodes.node_remote_lane_start_effect.tests.recorded_runner_steps import (
    RECORDED_DRAIN_SCRIPT,
    RECORDED_GUARD_SOURCE,
    RECORDED_GUARD_UNIT,
    RECORDED_GUARD_UNIT_FOR_UNSAFE_NAMES,
    RECORDED_REAP_SCRIPT,
)

pytestmark = pytest.mark.unit

REMOTE = ModelLaneHostRef(name="h-test", target="h-test.example.invalid")
STOP_LINE = 'LANE-GUARD-STOP {"systemd_stopped": true, "killed": [{"pid": 41}]}'
SPOOL_LINES = (
    'LANE-GUARD-SPOOL {"pid": 7, "reason": "runaway"}',
    'LANE-GUARD-SPOOL {"pid": 9, "reason": "memory"}',
)


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


def step() -> ModelLaneHostStepRequest:
    return ModelLaneHostStepRequest(
        host=REMOTE,
        lane="lane-example",
        run_id="rlane-example-000001",
        timeout_seconds=30,
    )


def transport(
    runner: ProtocolHostCommandRunner, source: str = RECORDED_GUARD_SOURCE
) -> SshLaneHostTransport:
    return SshLaneHostTransport(
        ssh_argv=["ssh"],
        host_run_root="/srv/example/runs",
        call_timeout_seconds=30,
        detach_argv=["setsid"],
        guard_host_source=source,
        runner=runner,
    )


def test_guard_unit_name_is_the_runner_unit_name() -> None:
    assert (
        guard_unit_name("lane-example", "rlane-example-000001") == RECORDED_GUARD_UNIT
    )
    lane, run_id, unit = RECORDED_GUARD_UNIT_FOR_UNSAFE_NAMES
    assert guard_unit_name(lane, run_id) == unit


def test_stop_lane_guard_sends_the_recorded_runner_reap_script() -> None:
    runner = Recorder(ModelHostCommandResult(exit_code=0, stdout=STOP_LINE + "\n"))
    answered = transport(runner).stop_lane_guard(step())
    assert runner.argvs == [["ssh", "h-test.example.invalid", RECORDED_REAP_SCRIPT]]
    assert answered.ok is True
    assert answered.cells == (
        ("stop", '{"systemd_stopped": true, "killed": [{"pid": 41}]}'),
    )


def test_stop_lane_guard_names_a_host_that_printed_no_stop_line() -> None:
    answered = transport(
        Recorder(ModelHostCommandResult(exit_code=0, stdout="nothing\n"))
    ).stop_lane_guard(step())
    assert answered.ok is False
    assert answered.error


def test_stop_lane_guard_names_a_failing_host_command() -> None:
    answered = transport(
        Recorder(ModelHostCommandResult(exit_code=255, stderr="ssh: refused"))
    ).stop_lane_guard(step())
    assert answered.ok is False
    assert "ssh: refused" in (answered.error or "")


def test_drain_guard_spool_sends_the_recorded_runner_drain_script() -> None:
    runner = Recorder(
        ModelHostCommandResult(exit_code=0, stdout="\n".join(SPOOL_LINES) + "\n")
    )
    answered = transport(runner).drain_guard_spool(step())
    assert runner.argvs == [["ssh", "h-test.example.invalid", RECORDED_DRAIN_SCRIPT]]
    assert answered.ok is True
    assert answered.cells == (
        ("spool", '{"pid": 7, "reason": "runaway"}'),
        ("spool", '{"pid": 9, "reason": "memory"}'),
    )


def test_drain_guard_spool_with_nothing_queued_returns_no_cells() -> None:
    answered = transport(
        Recorder(ModelHostCommandResult(exit_code=0, stdout=""))
    ).drain_guard_spool(step())
    assert answered.ok is True
    assert answered.cells == ()


def test_drain_guard_spool_names_a_failing_host_command() -> None:
    answered = transport(
        Recorder(ModelHostCommandResult(exit_code=1, stderr="no python3"))
    ).drain_guard_spool(step())
    assert answered.ok is False
    assert "no python3" in (answered.error or "")


@pytest.mark.parametrize("op", ["stop_lane_guard", "drain_guard_spool"])
def test_a_guard_step_without_the_host_program_runs_no_host_command(op: str) -> None:
    runner = Recorder(ModelHostCommandResult(exit_code=0, stdout=STOP_LINE))
    answered = getattr(transport(runner, source=""), op)(step())
    assert runner.argvs == []
    assert answered.ok is False
    assert "guard_host_source" in (answered.error or "")


def test_the_guard_scripts_run_the_injected_program_on_a_real_shell() -> None:
    """The recorded script, run by sh with a program that prints the lines the guard prints."""
    program = (
        "import sys\n"
        'print(\'LANE-GUARD-STOP {"systemd_stopped": false, "killed": []}\' '
        "if sys.argv[1] == 'reap' else 'LANE-GUARD-SPOOL {\"pid\": 3}')\n"
    )
    local = ModelLaneHostRef(name="h-local", local=True)
    real = SshLaneHostTransport(
        ssh_argv=["ssh"],
        host_run_root="/tmp/example-runs",
        call_timeout_seconds=30,
        detach_argv=["setsid"],
        guard_host_source=program,
    )
    request = ModelLaneHostStepRequest(
        host=local,
        lane="lane-example",
        run_id="rlane-example-000001",
        timeout_seconds=30,
    )
    assert (
        subprocess.run(["sh", "-c", "command -v python3"], check=False).returncode == 0
    )
    assert real.stop_lane_guard(request).cells == (
        ("stop", '{"systemd_stopped": false, "killed": []}'),
    )
    assert real.drain_guard_spool(request).cells == (("spool", '{"pid": 3}'),)
