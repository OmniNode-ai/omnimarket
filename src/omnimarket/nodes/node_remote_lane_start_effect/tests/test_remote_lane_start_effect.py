# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lane start effect: the handler, the scripted transport and the ssh transport on this machine (OMN-22848)."""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Sequence
from pathlib import Path

import pytest
import yaml
from omnibase_spi.contracts.services import (
    ModelLaneHostRef,
    ModelLaneReadEngineExitRequest,
    ModelLaneReadEngineExitResult,
    ModelLaneReadLogTailRequest,
    ModelLaneStartEngineRequest,
    ModelLaneStartEngineResult,
    ModelLaneStopEngineRequest,
)
from omnibase_spi.protocols.lane import ProtocolLaneHostTransport
from pydantic import ValidationError

from omnimarket.nodes.node_remote_lane_start_effect.adapters.adapter_scripted_transport import (
    ModelScriptedTransportScript,
    ScriptedLaneHostTransport,
)
from omnimarket.nodes.node_remote_lane_start_effect.adapters.adapter_ssh_transport import (
    ModelHostCommandResult,
    SshLaneHostTransport,
)
from omnimarket.nodes.node_remote_lane_start_effect.handlers.handler_remote_lane_start_effect import (
    HandlerRemoteLaneStartEffect,
)
from omnimarket.nodes.node_remote_lane_start_effect.models.model_remote_lane_host_call import (
    EnumLaneHostOperation,
    ModelRemoteLaneHostCall,
)

pytestmark = [pytest.mark.unit]

NODE_DIR = Path(__file__).resolve().parents[1]
LOCAL = ModelLaneHostRef(name="self", local=True)
SETSID_SHIM = (
    sys.executable,
    "-c",
    "import os,sys;os.setsid();os.execvp(sys.argv[1],sys.argv[1:])",
)


def _ssh(tmp: Path) -> SshLaneHostTransport:
    return SshLaneHostTransport(
        ssh_argv=("ssh",),
        host_run_root=str(tmp / "runs"),
        call_timeout_seconds=30,
        detach_argv=SETSID_SHIM,
    )


def _start(
    transport: SshLaneHostTransport, tmp: Path, argv: tuple[str, ...], run: str = "r1"
) -> ModelLaneStartEngineResult:
    return transport.start_engine_detached(
        ModelLaneStartEngineRequest(
            host=LOCAL,
            lane="l",
            run_id=run,
            workdir=str(tmp / "work"),
            engine_argv=argv,
            environment=(("LANE_X", "1"),),
            timeout_seconds=30,
        )
    )


def _wait_exit(
    transport: SshLaneHostTransport, run: str = "r1", budget: float = 20.0
) -> ModelLaneReadEngineExitResult:
    end = time.monotonic() + budget
    while time.monotonic() < end:
        state = transport.read_engine_exit(
            ModelLaneReadEngineExitRequest(host=LOCAL, run_id=run)
        )
        if not state.running:
            return state
        time.sleep(0.1)
    raise AssertionError("engine did not end")


def test_both_transports_satisfy_the_spi_protocol(tmp_path: Path) -> None:
    assert isinstance(
        ScriptedLaneHostTransport(ModelScriptedTransportScript()),
        ProtocolLaneHostTransport,
    )
    assert isinstance(_ssh(tmp_path), ProtocolLaneHostTransport)


def test_handler_routes_each_operation_to_the_transport() -> None:
    calls: list[str] = []
    handler = HandlerRemoteLaneStartEffect(
        ScriptedLaneHostTransport(ModelScriptedTransportScript(), calls)
    )
    result = handler.handle(
        ModelRemoteLaneHostCall(
            operation=EnumLaneHostOperation.READ_ENGINE_EXIT,
            engine_exit=ModelLaneReadEngineExitRequest(host=LOCAL, run_id="r"),
        )
    )
    assert result.engine_exit is not None
    assert result.engine_exit.exit_code == 0
    assert calls == ["host:read_engine_exit host=self"]


def test_a_call_carrying_the_wrong_request_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelRemoteLaneHostCall(
            operation=EnumLaneHostOperation.STOP_ENGINE,
            engine_exit=ModelLaneReadEngineExitRequest(host=LOCAL, run_id="r"),
        )


def test_a_handler_with_no_transport_refuses() -> None:
    with pytest.raises(RuntimeError, match="no host transport"):
        HandlerRemoteLaneStartEffect().handle(
            ModelRemoteLaneHostCall(
                operation=EnumLaneHostOperation.READ_ENGINE_EXIT,
                engine_exit=ModelLaneReadEngineExitRequest(host=LOCAL, run_id="r"),
            )
        )


def test_ssh_transport_starts_reads_log_and_exit(tmp_path: Path) -> None:
    transport = _ssh(tmp_path)
    started = _start(
        transport, tmp_path, ("sh", "-c", 'echo one; echo "x=$LANE_X"; exit 3')
    )
    assert started.state == "started"
    assert started.session_id
    assert _wait_exit(transport).exit_code == 3
    tail = transport.read_log_tail(
        ModelLaneReadLogTailRequest(host=LOCAL, run_id="r1", offset=0)
    )
    assert tail.lines == ("one", "x=1")
    assert tail.next_offset == 2
    again = transport.read_log_tail(
        ModelLaneReadLogTailRequest(host=LOCAL, run_id="r1", offset=2)
    )
    assert again.lines == ()
    assert again.next_offset == 2


def test_ssh_transport_engine_runs_in_its_own_session(tmp_path: Path) -> None:
    transport = _ssh(tmp_path)
    facts = tmp_path / "sid"
    started = _start(
        transport,
        tmp_path,
        (
            sys.executable,
            "-c",
            f"import os;open({str(facts)!r},'w').write(str(os.getsid(0)))",
        ),
    )
    assert started.state == "started"
    _wait_exit(transport)
    assert int(facts.read_text()) != os.getsid(0)
    assert int(facts.read_text()) == int(started.session_id or 0)


def test_ssh_transport_stop_confirms_the_exit(tmp_path: Path) -> None:
    transport = _ssh(tmp_path)
    assert _start(transport, tmp_path, ("sleep", "300")).state == "started"
    stopped = transport.stop_engine(
        ModelLaneStopEngineRequest(host=LOCAL, run_id="r1", grace_seconds=2)
    )
    assert stopped.confirmed
    assert stopped.exit_code == 143
    assert not transport.read_engine_exit(
        ModelLaneReadEngineExitRequest(host=LOCAL, run_id="r1")
    ).running


def test_ssh_transport_start_that_does_not_settle_is_unresolved(tmp_path: Path) -> None:
    class Stalled:
        def run(
            self, argv: Sequence[str], timeout_seconds: float
        ) -> ModelHostCommandResult:
            return ModelHostCommandResult(exit_code=124, stderr="timed out")

    transport = SshLaneHostTransport(
        ssh_argv=("ssh",),
        host_run_root=str(tmp_path),
        call_timeout_seconds=1,
        detach_argv=("setsid",),
        runner=Stalled(),
    )
    assert _start(transport, tmp_path, ("true",)).state == "unresolved"


def test_ssh_transport_remote_host_goes_through_the_ssh_argv(tmp_path: Path) -> None:
    seen: list[list[str]] = []

    class Record:
        def run(
            self, argv: Sequence[str], timeout_seconds: float
        ) -> ModelHostCommandResult:
            seen.append(list(argv))
            return ModelHostCommandResult(exit_code=0, stdout="RUNNING\n")

    transport = SshLaneHostTransport(
        ssh_argv=("ssh", "-o", "BatchMode=yes"),
        host_run_root="/runs",
        call_timeout_seconds=1,
        detach_argv=("setsid",),
        runner=Record(),
    )
    transport.read_engine_exit(
        ModelLaneReadEngineExitRequest(
            host=ModelLaneHostRef(name="h106", target="h106.lab"), run_id="r"
        )
    )
    assert seen[0][:4] == ["ssh", "-o", "BatchMode=yes", "h106.lab"]


def test_the_contract_names_the_spi_transport() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))
    [dep] = contract["protocol_dependencies"]
    assert dep["protocol"].endswith(":ProtocolLaneHostTransport")
    assert dep["protocol"].startswith("omnibase_spi.")
