# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23502: the lane guard's host program is wired into the start nodes' ssh transport.

The program is the one copy omnimarket owns (``node_lane_guard_unit_effect``). ``build_real`` injects its text as
the transport's ``guard_host_source``, so ``stop_lane_guard`` and ``drain_guard_spool`` run in a real start. The
guard commands run here against this host: ``reap`` for a unit that does not exist, ``spool-drain`` over a spool
directory of the test's own (never the host reaper's queue).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from omnibase_spi.contracts.services import ModelLaneHostRef, ModelLaneHostStepRequest

from omnimarket.nodes.node_lane_guard_unit_effect import (
    lane_guard_host_source as source_module,
)
from omnimarket.nodes.node_lane_guard_unit_effect.lane_guard_host_source import (
    lane_guard_host_source,
)
from omnimarket.nodes.node_remote_lane_start_effect.adapters.adapter_ssh_transport import (
    SshLaneHostTransport,
)
from omnimarket.nodes.node_remote_lane_start_orchestrator.handlers.adapter_host_pool import (
    InMemoryLaneHostPool,
)
from omnimarket.nodes.node_remote_lane_start_orchestrator.handlers.handler_remote_lane_start import (
    build_real,
)
from omnimarket.nodes.node_remote_lane_start_orchestrator.tests import (
    lane_start_fixtures as fx,
)

pytestmark = pytest.mark.unit

LOCAL = ModelLaneHostRef(name="self", local=True)
# Private attributes read by name, as test_remote_lane_start_real_wiring does.
BUILD_ATTR = "_build"
LOGIN_ATTR = "_login"
GUARD_SOURCE_ATTR = "_guard_source"


def step(lane: str = "sn7-guard-wiring") -> ModelLaneHostStepRequest:
    return ModelLaneHostStepRequest(
        host=LOCAL, lane=lane, run_id="rlane-sn7-000001", timeout_seconds=60
    )


def transport_of(tmp_path: Path) -> Any:
    handler = build_real(
        fx.settings(),
        InMemoryLaneHostPool(fx.snapshot()),
        stop_requested=lambda: False,
        sleep=lambda _s: None,
        echo=lambda _line: None,
    )
    build = getattr(handler, BUILD_ATTR)
    leader = build(fx.request(ledger_path=str(tmp_path / "ledger.md")))
    return getattr(leader, LOGIN_ATTR)


def test_build_real_injects_the_guard_host_program_into_the_transport(
    tmp_path: Path,
) -> None:
    transport = transport_of(tmp_path)
    assert isinstance(transport, SshLaneHostTransport)
    assert getattr(transport, GUARD_SOURCE_ATTR) == lane_guard_host_source()


def test_stop_lane_guard_reaps_a_unit_that_does_not_exist_with_the_injected_program(
    tmp_path: Path,
) -> None:
    transport = SshLaneHostTransport(
        ssh_argv=("ssh",),
        host_run_root=str(tmp_path / "runs"),
        call_timeout_seconds=60,
        detach_argv=("setsid",),
        guard_host_source=lane_guard_host_source(),
    )
    done = transport.stop_lane_guard(step())
    assert done.ok is True, done.error
    (cell,) = done.cells
    stop = json.loads(cell[1])
    assert cell[0] == "stop"
    assert stop["killed"] == []
    assert stop["unit"].startswith("onexlane-sn7-guard-wiring-")


def test_drain_guard_spool_prints_the_queued_records_of_the_test_spool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    spool = state / "onex-lane-guard"
    spool.mkdir(parents=True)
    (spool / "friction-spool.jsonl").write_text(
        json.dumps({"pid": 7, "reason": "runaway"}) + "\n", encoding="utf-8"
    )
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    transport = SshLaneHostTransport(
        ssh_argv=("ssh",),
        host_run_root=str(tmp_path / "runs"),
        call_timeout_seconds=60,
        detach_argv=("setsid",),
        guard_host_source=lane_guard_host_source(),
    )
    done = transport.drain_guard_spool(step())
    assert done.ok is True, done.error
    assert [json.loads(c[1]) for c in done.cells] == [{"pid": 7, "reason": "runaway"}]
    assert not (spool / "friction-spool.jsonl").exists()


def test_a_guard_program_that_lost_a_command_the_nodes_send_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = tmp_path / "runtime_lane_guard_host.py"
    broken.write_text("print('guard host program')\n", encoding="utf-8")
    monkeypatch.setattr(source_module, "HOST_PROGRAM", str(broken))
    monkeypatch.setattr(source_module, "__file__", str(tmp_path / "x.py"))
    with pytest.raises(ValueError, match="missing command"):
        lane_guard_host_source()
