# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains for the host bring-up run (OMN-21899).

Golden: the committed 57-target graph from a host whose person steps are done
reaches `up`, every step event on the bus reads ok after, and the one terminal is
completed. Error: the same run with one unit install failing emits the steps up to
it, one failed terminal naming that target, and frees the lease.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_host_bringup_orchestrator.handlers.handler_host_bringup_run import (
    HandlerHostBringupRun,
)
from omnimarket.nodes.node_host_bringup_orchestrator.handlers.inmemory_bringup_backends import (
    InMemoryBringupBus,
    InMemoryBringupHost,
    InMemoryBringupLease,
    InMemoryClock,
)
from omnimarket.nodes.node_host_bringup_orchestrator.models.model_host_bringup_run import (
    EnumRunOutcome,
    ModelHostBringupRunRequest,
    ModelHostBringupRunResult,
)
from omnimarket.nodes.node_host_bringup_plan_compute.handlers.handler_host_bringup_plan import (
    load_bringup_graph,
)

pytestmark = pytest.mark.unit

_CONTRACT = (
    Path(__file__).resolve().parent.parent
    / "src/omnimarket/nodes/node_host_bringup_orchestrator/contract.yaml"
)


def _run(
    fail: set[str],
) -> tuple[ModelHostBringupRunResult, InMemoryBringupBus, InMemoryBringupLease]:
    graph = load_bringup_graph(yaml.safe_load(_CONTRACT.read_text(encoding="utf-8")))
    done = {t.name for t in graph.targets if t.action.value in {"person_step", "stop"}}
    clock = InMemoryClock(now=1000)
    host = InMemoryBringupHost(satisfied=done, clock=clock, fail_actions=fail)
    lease = InMemoryBringupLease(clock=clock)
    bus = InMemoryBringupBus()
    request = ModelHostBringupRunRequest(
        host="hgolden",
        run_id="golden-1",
        graph=graph,
        top_target="up",
        lease_grace_s=600,
        action_timeout_s=300,
        wait_timeout_s=0,
        poll_interval_s=5,
        max_passes=80,
    )
    result = HandlerHostBringupRun(
        probe=host, actuator=host, lease=lease, bus=bus, clock=clock
    ).handle(request)
    return result, bus, lease


def test_golden_chain_up_on_the_committed_graph() -> None:
    result, bus, lease = _run(set())

    assert result.exit_code == 0
    assert [t.outcome for t in bus.terminals] == [EnumRunOutcome.COMPLETED]
    assert bus.steps
    assert {s.after.value for s in bus.steps} == {"ok"}
    assert bus.steps[-1].target == "in-service"
    assert lease.holder is None


def test_error_chain_failed_unit_install_ends_the_run() -> None:
    result, bus, lease = _run({"host-units"})

    assert result.exit_code == 1
    assert result.failed_target == "host-units"
    assert [t.outcome for t in bus.terminals] == [EnumRunOutcome.FAILED]
    assert "in-service" not in [s.target for s in bus.steps]
    assert "in-service" in result.remaining
    assert lease.holder is None
