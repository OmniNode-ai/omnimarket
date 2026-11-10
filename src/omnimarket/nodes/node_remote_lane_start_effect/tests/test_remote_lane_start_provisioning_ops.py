# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The provisioning steps of ProtocolLaneHostTransport on both transports (OMN-22848).

The spi protocol carries 15 host steps beside the five engine calls. The scripted transport logs
each one in order and answers ok, or refuses every one with the script's ``provision_error``; the
ssh transport does not run them (the frozen script still does) and answers each with a typed
refusal that names the step, without any host command.
"""

from __future__ import annotations

import inspect
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from omnibase_spi.contracts.services import (
    ModelLaneHostRef,
    ModelLaneHostStepRequest,
    ModelLaneShipPluginTreesRequest,
    ModelLaneStageInputRequest,
)
from omnibase_spi.protocols.lane import ProtocolLaneHostTransport

from omnimarket.nodes.node_remote_lane_start_effect.adapters.adapter_scripted_transport import (
    ModelScriptedTransportScript,
    ScriptedLaneHostTransport,
)
from omnimarket.nodes.node_remote_lane_start_effect.adapters.adapter_ssh_transport import (
    ModelHostCommandResult,
    SshLaneHostTransport,
)

pytestmark = [pytest.mark.unit]

LOCAL = ModelLaneHostRef(name="self", local=True)
ENGINE_CALLS = frozenset(
    {
        "prepare_worktree",
        "start_engine_detached",
        "read_engine_exit",
        "stop_engine",
        "read_log_tail",
    }
)
STAGE_OPS = frozenset({"stage_ledger_snapshot", "stage_file"})
#: Steps the ssh transport now runs itself (OMN-23425): their parity with the runner is in
#: test_remote_lane_start_provisioning_parity.py. resolve_registry_root stays here: with no candidates
#: or marker configured it still answers "not run".
MOVED_OPS = frozenset(
    {
        "prepare_run_dirs",
        "read_ledger_outbox",
        "read_lane_changes",
        "hold_run_dir_guard",
        "drain_guard_spool",
        "stop_lane_guard",
    }
)
PROVISIONING_OPS = tuple(
    name
    for name, _ in inspect.getmembers(ProtocolLaneHostTransport, inspect.isfunction)
    if not name.startswith("_") and name not in ENGINE_CALLS
)
UNMOVED_OPS = tuple(op for op in PROVISIONING_OPS if op not in MOVED_OPS)


def _request(op: str) -> Any:
    base: dict[str, Any] = {
        "host": LOCAL,
        "lane": "l",
        "run_id": "r1",
        "timeout_seconds": 5,
    }
    if op in STAGE_OPS:
        return ModelLaneStageInputRequest(**base, name="f", content="x")
    if op == "ship_plugin_trees":
        return ModelLaneShipPluginTreesRequest(**base)
    return ModelLaneHostStepRequest(**base)


def _error_of(result: Any) -> str | None:
    error: str | None = result.error
    return error


class _NoHostCommand:
    def __init__(self) -> None:
        self.argvs: list[Sequence[str]] = []

    def run(self, argv: Sequence[str], timeout: float) -> ModelHostCommandResult:
        self.argvs.append(argv)
        raise AssertionError(
            "a provisioning step the ssh transport does not run issued a host command"
        )


def test_the_protocol_carries_the_fifteen_provisioning_steps() -> None:
    assert len(PROVISIONING_OPS) == 15
    assert "resolve_registry_root" in PROVISIONING_OPS
    assert "stop_lane_guard" in PROVISIONING_OPS


def test_a_transport_missing_one_step_is_not_the_protocol() -> None:
    full = ScriptedLaneHostTransport(ModelScriptedTransportScript())
    short = SimpleNamespace(
        **{
            name: getattr(full, name)
            for name in dir(full)
            if not name.startswith("_") and name != "stop_lane_guard"
        }
    )

    assert not isinstance(short, ProtocolLaneHostTransport)


def test_scripted_transport_logs_every_provisioning_step_in_order() -> None:
    calls: list[str] = []
    transport = ScriptedLaneHostTransport(ModelScriptedTransportScript(), calls)
    for op in PROVISIONING_OPS:
        result = getattr(transport, op)(_request(op))
        assert _error_of(result) is None, op
    assert [c.split(" ")[0] for c in calls] == [f"host:{op}" for op in PROVISIONING_OPS]


@pytest.mark.parametrize("op", PROVISIONING_OPS)
def test_scripted_provision_error_refuses_each_step_with_a_typed_result(
    op: str,
) -> None:
    transport = ScriptedLaneHostTransport(
        ModelScriptedTransportScript(provision_error="host refused")
    )
    result = getattr(transport, op)(_request(op))
    assert _error_of(result) == "host refused"


@pytest.mark.parametrize("op", UNMOVED_OPS)
def test_ssh_transport_refuses_each_unmoved_step_by_name_without_a_host_command(
    op: str, tmp_path: Path
) -> None:
    runner = _NoHostCommand()
    transport = SshLaneHostTransport(
        ssh_argv=("ssh",),
        host_run_root=str(tmp_path / "runs"),
        call_timeout_seconds=30,
        detach_argv=("setsid",),
        runner=runner,
    )
    result = getattr(transport, op)(_request(op))
    error = _error_of(result)
    assert error is not None
    assert error.startswith(f"{op}: not run by the ssh transport")
    assert runner.argvs == []
