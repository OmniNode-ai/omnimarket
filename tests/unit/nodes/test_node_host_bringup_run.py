# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The host bring-up run (OMN-21899), on in-memory backends.

Each test drives HandlerHostBringupRun over the fixture graph with an in-memory
host, lease, bus and clock, and holds one of the conditions of the OMN-21882
design-review verdict (formal/host_bringup/results/design_review_verdict.txt in
omnibase_infra) or one exit code of plan section 4.4.
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
)
from omnimarket.nodes.node_host_bringup_plan_compute.handlers.handler_host_bringup_plan import (
    load_bringup_graph,
)
from omnimarket.nodes.node_host_bringup_plan_compute.models import ModelBringupGraph

pytestmark = pytest.mark.unit

_FIXTURE = (
    Path(__file__).resolve().parents[2] / "fixtures/host_bringup/fixture_graph.yaml"
)
_ALL = (
    "os_packages",
    "operator_sudo",
    "tailnet_joined",
    "runtime_env_file",
    "container_engine",
    "runtime_stack",
)


def _graph() -> ModelBringupGraph:
    return load_bringup_graph(yaml.safe_load(_FIXTURE.read_text(encoding="utf-8")))


def _request(run_id: str = "run-1", **kw: object) -> ModelHostBringupRunRequest:
    fields: dict[str, object] = {
        "host": "hfixture",
        "run_id": run_id,
        "graph": _graph(),
        "top_target": "runtime_stack",
        "lease_grace_s": 30,
        "action_timeout_s": 10,
        "wait_timeout_s": 20,
        "poll_interval_s": 5,
        "max_passes": 20,
    }
    fields.update(kw)
    return ModelHostBringupRunRequest.model_validate(fields)


def _world(
    satisfied: tuple[str, ...] = ("operator_sudo",),
) -> tuple[
    InMemoryBringupHost, InMemoryBringupLease, InMemoryBringupBus, InMemoryClock
]:
    clock = InMemoryClock(now=100)
    return (
        InMemoryBringupHost(satisfied=set(satisfied), clock=clock),
        InMemoryBringupLease(clock=clock),
        InMemoryBringupBus(),
        clock,
    )


def _handler(
    host: InMemoryBringupHost,
    lease: InMemoryBringupLease,
    bus: InMemoryBringupBus,
    clock: InMemoryClock,
) -> HandlerHostBringupRun:
    return HandlerHostBringupRun(
        probe=host, actuator=host, lease=lease, bus=bus, clock=clock
    )


def test_up_from_bare_host_exits_0_and_acts_needs_first() -> None:
    host, lease, bus, clock = _world()
    result = _handler(host, lease, bus, clock).handle(_request())

    assert result.exit_code == 0
    assert result.outcome is EnumRunOutcome.COMPLETED
    assert set(host.satisfied) == set(_ALL)
    acted = [s.target for s in result.steps]
    assert acted.index("os_packages") < acted.index("container_engine")
    assert acted[-1] == "runtime_stack"
    assert [t.run_id for t in bus.terminals] == ["run-1"]
    assert lease.holder is None


def test_satisfied_targets_are_skipped_and_a_rerun_changes_nothing() -> None:
    host, lease, bus, clock = _world()
    handler = _handler(host, lease, bus, clock)
    handler.handle(_request("run-1"))
    acts_before = list(host.acts)
    again = handler.handle(_request("run-2"))

    assert again.exit_code == 0
    assert again.steps == ()
    assert host.acts == acts_before


def test_failed_action_stops_the_run_exit_1_and_prints_what_remains() -> None:
    host, lease, bus, clock = _world()
    host.fail_actions.add("container_engine")
    result = _handler(host, lease, bus, clock).handle(_request())

    assert result.exit_code == 1
    assert result.outcome is EnumRunOutcome.FAILED
    assert result.failed_target == "container_engine"
    assert "runtime_stack" in result.remaining
    assert "runtime_stack" not in host.acts
    assert [t.outcome for t in bus.terminals] == [EnumRunOutcome.FAILED]


def test_wait_timeout_ends_the_run_exit_75_and_never_passes() -> None:
    """Verdict condition 5: a wait that reaches wait_timeout_s ends the run."""
    host, lease, bus, clock = _world(satisfied=())
    result = _handler(host, lease, bus, clock).handle(_request())

    assert result.exit_code == 75
    assert result.outcome is EnumRunOutcome.WAITING
    assert result.person_line is not None
    assert "operator_sudo" in result.person_line
    assert "operator_sudo" not in host.satisfied
    assert "tailnet_joined" not in host.acts
    assert "container_engine" not in host.acts


def test_a_wait_carries_on_when_the_person_step_lands_inside_the_timeout() -> None:
    host, lease, bus, clock = _world(satisfied=())
    host.person_lands_after_s["operator_sudo"] = 10
    result = _handler(host, lease, bus, clock).handle(_request())

    assert result.exit_code == 0
    assert set(host.satisfied) == set(_ALL)


def test_no_wait_exits_75_at_the_first_person_step() -> None:
    host, lease, bus, clock = _world(satisfied=())
    host.person_lands_after_s["operator_sudo"] = 10
    result = _handler(host, lease, bus, clock).handle(_request(no_wait=True))

    assert result.exit_code == 75
    assert clock.now == 100


def test_unknown_top_target_is_usage_exit_2_with_no_lease_taken() -> None:
    host, lease, bus, clock = _world()
    result = _handler(host, lease, bus, clock).handle(_request(top_target="nope"))

    assert result.exit_code == 2
    assert result.outcome is EnumRunOutcome.USAGE
    assert lease.fence == 0
    assert host.acts == []


def test_second_run_while_lease_held_exits_3() -> None:
    host, lease, bus, clock = _world()
    lease.grant("hfixture", "run-other", until=clock.now + 60)
    result = _handler(host, lease, bus, clock).handle(_request("run-1"))

    assert result.exit_code == 3
    assert result.outcome is EnumRunOutcome.LEASE_HELD
    assert host.acts == []
    assert lease.holder == "run-other"


def test_takeover_closes_every_owed_run_abandoned_once() -> None:
    """Verdict condition 1: the lease carries the set of runs owed a terminal."""
    host, lease, bus, clock = _world()
    lease.grant("hfixture", "run-a", until=clock.now + 1)
    lease.owed = {"run-z"}
    clock.now += 5
    result = _handler(host, lease, bus, clock).handle(_request("run-b"))

    assert result.exit_code == 0
    assert sorted(result.abandoned_run_ids) == ["run-a", "run-z"]
    by_run = {t.run_id: t.outcome for t in bus.terminals}
    assert by_run == {
        "run-a": EnumRunOutcome.ABANDONED,
        "run-z": EnumRunOutcome.ABANDONED,
        "run-b": EnumRunOutcome.COMPLETED,
    }
    assert lease.owed == set()


def test_terminal_is_if_absent_on_the_run_id() -> None:
    """Verdict condition 2: a second emit for one run id adds nothing."""
    host, lease, bus, clock = _world()
    handler = _handler(host, lease, bus, clock)
    handler.handle(_request("run-1"))
    handler.handle(_request("run-1"))

    assert [t.run_id for t in bus.terminals] == ["run-1"]


def test_terminal_is_emitted_before_the_lease_is_released() -> None:
    """Verdict condition 3: terminal first, then release."""
    host, lease, bus, clock = _world()
    _handler(host, lease, bus, clock).handle(_request())

    order = [e for e in bus.journal + lease.journal if e[1] in {"terminal", "release"}]
    order.sort(key=lambda e: e[0])
    assert [e[1] for e in order] == ["terminal", "release"]


def test_external_action_renews_the_lease_to_start_plus_grace() -> None:
    """Verdict condition 4: each external action renews before it starts."""
    host, lease, bus, clock = _world()
    _handler(host, lease, bus, clock).handle(_request())

    assert host.acts
    assert len(lease.renewals) >= len(host.acts)
    for start, until in lease.renewals:
        assert until >= start + 30


def test_grace_not_above_action_timeout_is_refused() -> None:
    with pytest.raises(ValueError, match="lease_grace_s"):
        _request(lease_grace_s=10, action_timeout_s=10)


def test_stale_holder_is_refused_inside_the_lock_and_never_completes() -> None:
    """A successor took the lease mid-run: the stale run acts no further."""
    host, lease, bus, clock = _world()
    host.on_act["os_packages"] = lambda: lease.grant(
        "hfixture", "run-succ", until=clock.now + 999
    )
    result = _handler(host, lease, bus, clock).handle(_request("run-stale"))

    assert result.exit_code == 3
    assert result.outcome is EnumRunOutcome.REFUSED_STALE
    assert host.acts == ["os_packages"]
    assert lease.holder == "run-succ"
    assert all(t.outcome is not EnumRunOutcome.COMPLETED for t in bus.terminals)


def test_plan_uses_only_the_reads_of_the_current_pass() -> None:
    """A target that regresses after it read ok is acted on again, needs first."""
    host, lease, bus, clock = _world()
    host.on_act["container_engine"] = lambda: host.satisfied.discard("os_packages")
    result = _handler(host, lease, bus, clock).handle(_request())

    assert result.exit_code == 0
    assert host.acts.count("os_packages") == 2
    second = [i for i, t in enumerate(host.acts) if t == "os_packages"][1]
    assert host.acts.index("runtime_stack") > second


def test_every_step_and_the_terminal_are_on_the_bus() -> None:
    host, lease, bus, clock = _world()
    result = _handler(host, lease, bus, clock).handle(_request())

    assert [s.target for s in bus.steps] == [s.target for s in result.steps]
    assert all(s.after.value == "ok" for s in bus.steps)
    assert bus.terminals[0].fence == result.fence


_CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_host_bringup_orchestrator/contract.yaml"
)


def test_committed_graph_flips_the_host_row_last_and_reinstalls_units() -> None:
    """On the 57-target graph the in-service flip is the last action of `up`.

    Person steps and stop rows read satisfied (a person did them, or the
    declaration holds); every other target is acted on once, the unit installs
    of a reinstall (host-units, lab-work-serve) included.
    """
    graph = load_bringup_graph(yaml.safe_load(_CONTRACT.read_text(encoding="utf-8")))
    person = {
        t.name for t in graph.targets if t.action.value in {"person_step", "stop"}
    }
    clock = InMemoryClock(now=100)
    host = InMemoryBringupHost(satisfied=set(person), clock=clock)
    lease = InMemoryBringupLease(clock=clock)
    bus = InMemoryBringupBus()
    result = HandlerHostBringupRun(
        probe=host, actuator=host, lease=lease, bus=bus, clock=clock
    ).handle(_request(graph=graph, top_target="up", max_passes=80))

    assert result.exit_code == 0, result.detail
    assert host.acts[-1] == "in-service"
    assert {"host-units", "lab-work-serve", "declared"} <= set(host.acts)
    assert "storage-up" not in host.acts
    assert "data-moved" not in host.acts
