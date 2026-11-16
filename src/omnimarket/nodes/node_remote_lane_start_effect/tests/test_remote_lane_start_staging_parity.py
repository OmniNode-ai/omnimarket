# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Parity of the ssh transport's staging steps with the frozen runner script (OMN-23425).

``stage_ledger_snapshot``, ``stage_file`` and ``write_runtime_config`` send the host the text the runner's
``ledger_unpack_script``, ``put_file`` and ``runtime_config_script`` built for the recorded lane
(``recorded_runner_steps``), carry the staged bytes on stdin, and read back the same printed lines. A change to
the transport's script or to the recorded text fails these.
"""

from __future__ import annotations

import base64
from collections.abc import Sequence

import pytest
from omnibase_spi.contracts.services import (
    ModelLaneHostRef,
    ModelLaneHostStepRequest,
    ModelLaneStageInputRequest,
)

from omnimarket.nodes.node_remote_lane_start_effect.adapters.adapter_ssh_transport import (
    ModelHostCommandResult,
    SshLaneHostTransport,
)
from omnimarket.nodes.node_remote_lane_start_effect.tests.recorded_runner_steps import (
    RECORDED_LEDGER_UNPACK_BUS_SCRIPT,
    RECORDED_LEDGER_UNPACK_SCRIPT,
    RECORDED_PUT_FILE_SCRIPT,
    RECORDED_RUNTIME_CONFIG_SCRIPT,
)

pytestmark = pytest.mark.unit

REMOTE = ModelLaneHostRef(name="h-test", target="h-test.example.invalid")
RUN_ID = "rlane-example-000001"
LEDGER = "example-ledger.md"
ARCHIVE = b"\x1f\x8b\x08 not text \xff"
TARGET = "../checkout/config/onex/runtime/runtime_config.yaml"
CONFIG = "kafka:\n  bootstrap: broker.example.invalid:9092\n"


class Recorder:
    def __init__(self, answer: ModelHostCommandResult) -> None:
        self.answer = answer
        self.argvs: list[list[str]] = []
        self.stdins: list[bytes | None] = []

    def run(
        self,
        argv: Sequence[str],
        timeout_seconds: float,
        stdin: bytes | None = None,
    ) -> ModelHostCommandResult:
        self.argvs.append(list(argv))
        self.stdins.append(stdin)
        return self.answer


def as_recorded(script: str) -> str:
    """The runner's recorded text with its home-relative run root swapped for the declared root."""
    return (
        script.replace('LR="$HOME/lab-run"; ', "")
        .replace("$HOME/lab-run/runs", "/srv/example/runs")
        .replace("$LR/runs", "/srv/example/runs")
    )


def transport(
    runner: Recorder, via: str = "local", config: str = CONFIG
) -> SshLaneHostTransport:
    return SshLaneHostTransport(
        ssh_argv=["ssh"],
        host_run_root="/srv/example/runs",
        call_timeout_seconds=30,
        detach_argv=["setsid"],
        ledger_write_via=via,
        runtime_config_text=config,
        runtime_config_target=TARGET,
        runner=runner,
    )


def stage(name: str, content: str, mode: int = 0o600) -> ModelLaneStageInputRequest:
    return ModelLaneStageInputRequest(
        host=REMOTE,
        lane="lane-example",
        run_id=RUN_ID,
        name=name,
        content=content,
        mode=mode,
        timeout_seconds=30,
    )


def step(
    registry_root: str | None = "/srv/example/registry",
) -> ModelLaneHostStepRequest:
    return ModelLaneHostStepRequest(
        host=REMOTE,
        lane="lane-example",
        run_id=RUN_ID,
        registry_root=registry_root,
        timeout_seconds=30,
    )


SNAPSHOT_OK = ModelHostCommandResult(
    exit_code=0,
    stdout="LEDGER-SNAPSHOT /srv/example/runs/rlane-example-000001.ctx/ledger\n",
)


def test_stage_ledger_snapshot_sends_the_recorded_unpack_script_and_the_decoded_archive() -> (
    None
):
    runner = Recorder(SNAPSHOT_OK)
    staged = transport(runner).stage_ledger_snapshot(
        stage(LEDGER, base64.b64encode(ARCHIVE).decode())
    )
    assert runner.argvs == [
        ["ssh", "h-test.example.invalid", as_recorded(RECORDED_LEDGER_UNPACK_SCRIPT)]
    ]
    assert runner.stdins == [ARCHIVE]
    assert staged.staged is True
    assert staged.path == "/srv/example/runs/rlane-example-000001.ctx/ledger"


def test_stage_ledger_snapshot_keeps_the_live_ledger_writable_when_the_bus_is_the_ledger() -> (
    None
):
    runner = Recorder(SNAPSHOT_OK)
    transport(runner, via="bus").stage_ledger_snapshot(
        stage(LEDGER, base64.b64encode(ARCHIVE).decode())
    )
    assert runner.argvs[0][-1] == as_recorded(RECORDED_LEDGER_UNPACK_BUS_SCRIPT)


def test_stage_ledger_snapshot_refuses_content_that_is_not_base64_without_a_host_command() -> (
    None
):
    runner = Recorder(SNAPSHOT_OK)
    staged = transport(runner).stage_ledger_snapshot(stage(LEDGER, "not base64 !!"))
    assert staged.staged is False
    assert staged.error is not None
    assert "base64" in staged.error
    assert runner.argvs == []


def test_stage_ledger_snapshot_names_the_host_failure() -> None:
    runner = Recorder(ModelHostCommandResult(exit_code=70, stderr="tar: short read"))
    staged = transport(runner).stage_ledger_snapshot(
        stage(LEDGER, base64.b64encode(ARCHIVE).decode())
    )
    assert staged.staged is False
    assert staged.error == "tar: short read"


def test_stage_file_sends_the_recorded_put_file_script_and_the_text_on_stdin() -> None:
    runner = Recorder(ModelHostCommandResult(exit_code=0))
    staged = transport(runner).stage_file(stage("prompt.md", "do the work\n"))
    assert runner.argvs == [
        ["ssh", "h-test.example.invalid", as_recorded(RECORDED_PUT_FILE_SCRIPT)]
    ]
    assert runner.stdins == [b"do the work\n"]
    assert staged.staged is True
    assert staged.path == "/srv/example/runs/rlane-example-000001.ctx/prompt.md"


def test_stage_file_adds_a_chmod_only_for_another_mode() -> None:
    runner = Recorder(ModelHostCommandResult(exit_code=0))
    transport(runner).stage_file(stage("guard.sh", "x", mode=0o700))
    assert 'chmod 700 "$T"' in runner.argvs[0][-1].splitlines()


@pytest.mark.parametrize("name", [".hidden", "-x", "a b", "a;b"])
def test_stage_file_refuses_a_name_that_is_not_one_plain_component(name: str) -> None:
    runner = Recorder(ModelHostCommandResult(exit_code=0))
    staged = transport(runner).stage_file(stage(name, "x"))
    assert staged.staged is False
    assert runner.argvs == []


@pytest.mark.parametrize("name", ["../x", "a/b", ""])
def test_the_request_model_itself_refuses_a_path_or_empty_name(name: str) -> None:
    with pytest.raises(ValueError, match="name"):
        stage(name, "x")


def test_write_runtime_config_sends_the_recorded_script_and_the_config_on_stdin() -> (
    None
):
    target = "/srv/example/registry/../checkout/config/onex/runtime/runtime_config.yaml"
    runner = Recorder(
        ModelHostCommandResult(
            exit_code=0, stdout=f"RUNTIME-CONFIG installed {target}\n"
        )
    )
    done = transport(runner).write_runtime_config(step())
    assert runner.argvs == [
        ["ssh", "h-test.example.invalid", RECORDED_RUNTIME_CONFIG_SCRIPT]
    ]
    assert runner.stdins == [CONFIG.encode()]
    assert done.ok is True
    assert done.cells == (("runtime_config", f"RUNTIME-CONFIG installed {target}"),)


def test_write_runtime_config_reports_a_host_copy_that_is_already_there() -> None:
    runner = Recorder(
        ModelHostCommandResult(
            exit_code=0, stdout="RUNTIME-CONFIG present /x/runtime_config.yaml\n"
        )
    )
    done = transport(runner).write_runtime_config(step())
    assert done.ok is True
    assert done.cells[0][1].startswith("RUNTIME-CONFIG present ")


def test_write_runtime_config_needs_the_registry_root() -> None:
    runner = Recorder(ModelHostCommandResult(exit_code=0))
    done = transport(runner).write_runtime_config(step(registry_root=None))
    assert done.ok is False
    assert done.error == "write_runtime_config: registry root not resolved"
    assert runner.argvs == []


def test_a_runtime_config_with_credential_like_text_is_refused_at_construction() -> (
    None
):
    with pytest.raises(ValueError, match="credential-like"):
        transport(
            Recorder(ModelHostCommandResult(exit_code=0)), config="sasl_password: x"
        )


def test_write_runtime_config_without_config_text_is_not_run() -> None:
    runner = Recorder(ModelHostCommandResult(exit_code=0))
    done = transport(runner, config="").write_runtime_config(step())
    assert done.ok is False
    assert done.error is not None
    assert done.error.startswith("write_runtime_config: not run")
    assert runner.argvs == []


def test_write_runtime_config_without_a_target_is_not_run() -> None:
    runner = Recorder(ModelHostCommandResult(exit_code=0))
    bare = SshLaneHostTransport(
        ssh_argv=["ssh"],
        host_run_root="/srv/example/runs",
        call_timeout_seconds=30,
        detach_argv=["setsid"],
        runtime_config_text=CONFIG,
        runner=runner,
    )
    done = bare.write_runtime_config(step())
    assert done.ok is False
    assert runner.argvs == []


@pytest.mark.parametrize("target", ["/abs/path", "a b/c", "../../up/x", "x;y"])
def test_a_runtime_config_target_that_escapes_or_is_not_a_plain_path_is_refused(
    target: str,
) -> None:
    with pytest.raises(ValueError, match="runtime_config_target"):
        SshLaneHostTransport(
            ssh_argv=["ssh"],
            host_run_root="/srv/example/runs",
            call_timeout_seconds=30,
            detach_argv=["setsid"],
            runtime_config_text=CONFIG,
            runtime_config_target=target,
        )
