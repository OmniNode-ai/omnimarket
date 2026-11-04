# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lane guard unit node starts a fixture engine, lists its members and stops it (OMN-22192).

The fixture engine starts a background child in its own session (``setsid``), the shape that survived a
lane's process-group kill on 2026-09-30. Nothing here is mocked: the node runs the guard program, the
guard tags the engine's environment, and the node reads the real process table.
"""

import os
import sys
import time

from omnimarket.nodes.node_lane_guard_unit_effect import (
    runtime_lane_guard_host as lane_guard_host,
)
from omnimarket.nodes.node_lane_guard_unit_effect.handlers.handler_lane_guard_unit import (
    HandlerLaneGuardUnit,
)
from omnimarket.nodes.node_lane_guard_unit_effect.models import (
    EnumLaneGuardOperation,
    ModelLaneGuardUnitRequest,
    ModelLaneGuardUnitResult,
)

ENGINE = (
    "import os, subprocess, sys, time\n"
    "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'], start_new_session=True)\n"
    "time.sleep(120)\n"
)


def _req(
    op: EnumLaneGuardOperation,
    run_id: str,
    command: tuple[str, ...] = (),
    timeout_s: int = 600,
    mem_max: str | None = None,
) -> ModelLaneGuardUnitRequest:
    return ModelLaneGuardUnitRequest(
        operation=op,
        lane="fixture-lane",
        run_id=run_id,
        command=command,
        timeout_s=timeout_s,
        mem_max=mem_max,
    )


def _alive(pid: int) -> bool:
    """True while the pid is a live process; a zombie awaiting its parent's wait is not."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as handle:
            return handle.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


def _wait_members(
    handler: HandlerLaneGuardUnit, run_id: str, count: int
) -> ModelLaneGuardUnitResult:
    deadline = time.monotonic() + 20
    result = handler.handle(_req(EnumLaneGuardOperation.LIST, run_id))
    while len(result.members) < count and time.monotonic() < deadline:
        time.sleep(0.2)
        result = handler.handle(_req(EnumLaneGuardOperation.LIST, run_id))
    return result


def test_start_list_stop_leaves_no_background_child() -> None:
    handler = HandlerLaneGuardUnit()
    run_id = f"unit-{time.time_ns()}"
    started = handler.handle(
        _req(
            EnumLaneGuardOperation.START, run_id, command=(sys.executable, "-c", ENGINE)
        )
    )
    unit = started.unit
    assert unit.startswith("onexlane-fixture-lane-")
    assert started.started_pid
    assert started.refusal is None

    listed = _wait_members(handler, run_id, 2)  # the engine and its setsid child
    assert len(listed.members) == 2, listed

    stopped = handler.handle(_req(EnumLaneGuardOperation.STOP, run_id))
    assert stopped.confirmed_empty is True
    # a systemd scope ends its members itself; otherwise the tag sweep kills them
    assert stopped.systemd_stopped or len(stopped.killed) == 2
    for member in listed.members:
        assert not _alive(member.pid), member
    assert handler.handle(_req(EnumLaneGuardOperation.LIST, run_id)).members == ()


def test_stop_of_an_unknown_unit_is_confirmed_empty() -> None:
    stopped = HandlerLaneGuardUnit().handle(
        _req(EnumLaneGuardOperation.STOP, f"never-{time.time_ns()}")
    )
    assert stopped.confirmed_empty is True
    assert stopped.killed == ()


def test_start_without_a_command_is_refused() -> None:
    result = HandlerLaneGuardUnit().handle(_req(EnumLaneGuardOperation.START, "r1"))
    assert result.refusal == "start needs a command"
    assert result.started_pid is None


def test_start_past_the_hard_cap_is_refused() -> None:
    result = HandlerLaneGuardUnit().handle(
        _req(
            EnumLaneGuardOperation.START,
            "r2",
            command=("true",),
            timeout_s=lane_guard_host.HARD_CAP_S,
        )
    )
    assert result.refusal
    assert "hard cap" in result.refusal
    assert result.started_pid is None


def test_start_past_the_memory_ceiling_is_refused() -> None:
    result = HandlerLaneGuardUnit().handle(
        _req(EnumLaneGuardOperation.START, "r3", command=("true",), mem_max="64G")
    )
    assert result.refusal
    assert "hard ceiling" in result.refusal


def test_unit_name_carries_the_lane_and_run() -> None:
    assert (
        lane_guard_host.unit_name("my lane", "run-abc123") == "onexlane-my-lane-abc123"
    )
