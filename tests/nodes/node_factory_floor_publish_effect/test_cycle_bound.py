# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22699: the contract names a cycle bound, and the timeouts it declares cannot add up past it."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from omnimarket.nodes.node_factory_floor_publish_effect.handlers.handler_delegation_report import (
    HandlerDelegationReport,
)
from omnimarket.nodes.node_factory_floor_publish_effect.handlers.handler_factory_floor_publish import (
    HandlerFactoryFloorPublish,
    PublishRefusedError,
    load_runtime,
)
from omnimarket.nodes.node_factory_floor_publish_effect.models.model_delegation_report import (
    ModelDelegationReadOverlay,
    ModelDelegationReportRequest,
)
from omnimarket.nodes.node_factory_floor_publish_effect.models.model_factory_floor_publish import (
    ModelFactoryFloorPublishRequest,
    ModelFactoryFloorPublishRuntime,
    ModelPublishStepResult,
)
from tests.nodes.node_factory_floor_publish_effect.test_factory_floor_publish import (
    PLUGIN,
    LocalSsh,
    _home,
    _overlay,
)

pytestmark = pytest.mark.unit


def _longest(rt: ModelFactoryFloorPublishRuntime) -> int:
    return sum(rt.declared_longest_path().values())


def _with_step_timeout(
    rt: ModelFactoryFloorPublishRuntime, name: str, seconds: int
) -> ModelFactoryFloorPublishRuntime:
    """The runtime with one step's declared timeout replaced (an input step or an after-publish step)."""
    steps = tuple(
        s.model_copy(update={"timeout_seconds": seconds}) if s.name == name else s
        for s in rt.steps
    )
    after = tuple(
        s.model_copy(update={"timeout_seconds": seconds}) if s.name == name else s
        for s in rt.after_publish_steps
    )
    return rt.model_copy(update={"steps": steps, "after_publish_steps": after})


def test_the_longest_declared_path_holds_under_the_cycle_bound() -> None:
    rt = load_runtime()
    path = rt.declared_longest_path()
    assert set(path) == {
        "input_steps",
        "delegation",
        "copy",
        "history_prune",
        "after_publish",
        "final_status",
    }
    assert rt.cycle.bound_seconds == 285
    assert _longest(rt) <= rt.cycle.bound_seconds, path


def test_concurrent_input_steps_count_at_their_slowest_and_sequential_ones_add_up() -> (
    None
):
    rt = load_runtime()
    timeouts = {s.name: s.timeout_seconds for s in rt.steps}
    concurrent = [s.timeout_seconds for s in rt.steps if s.concurrent]
    assert (
        len(concurrent) > 1
    )  # the group exists, so the slowest-member rule is exercised
    expected = (
        max(concurrent)
        + timeouts["factory_floor"]
        + timeouts["floor_simple"]
        + timeouts["floor_compare"]
    )
    assert rt.declared_longest_path()["input_steps"] == expected
    assert sum(timeouts.values()) > expected  # a plain sum would overstate the path


@pytest.mark.parametrize(
    "name",
    sorted(
        {s.name for s in load_runtime().steps}
        | {s.name for s in load_runtime().after_publish_steps}
    ),
)
def test_a_step_timeout_raised_past_the_bound_fails_the_bound(name: str) -> None:
    """The falsifier: any one step's declared timeout raised past the bound makes the longest path exceed it."""
    rt = load_runtime()
    raised = _with_step_timeout(rt, name, rt.cycle.bound_seconds + 1)
    assert _longest(raised) > rt.cycle.bound_seconds


@pytest.mark.parametrize(
    "field",
    [
        "delegation_timeout_seconds",
        "copy_budget_seconds",
        "history_prune_budget_seconds",
        "final_status_seconds",
    ],
)
def test_a_phase_budget_raised_past_the_bound_fails_the_bound(field: str) -> None:
    rt = load_runtime()
    cycle = rt.cycle.model_copy(update={field: rt.cycle.bound_seconds + 1})
    assert _longest(rt.model_copy(update={"cycle": cycle})) > rt.cycle.bound_seconds


class TimedSsh(LocalSsh):
    """LocalSsh that keeps the time limit each call was given."""

    def __init__(self) -> None:
        super().__init__()
        self.limits: list[tuple[str, int]] = []

    def __call__(
        self, argv: Sequence[str], stdin: bytes | None, timeout: int
    ) -> subprocess.CompletedProcess[bytes]:
        self.limits.append((argv[-1], timeout))
        return super().__call__(argv, stdin, timeout)


def _handle(
    tmp_path: Path, runner: LocalSsh, clock: Callable[[], float] | None = None
) -> tuple[object, Path]:
    home = _home(tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    request = ModelFactoryFloorPublishRequest(
        home=str(home), overlay=_overlay(tmp_path), state_dir=str(state)
    )
    handler = (
        HandlerFactoryFloorPublish(runner)
        if clock is None
        else HandlerFactoryFloorPublish(runner, clock=clock)
    )
    return handler.handle(
        request, environ=PLUGIN
    ), tmp_path / "site" / "publish-status.json"


def test_every_copy_prune_and_status_call_is_held_to_its_phase_limit(
    tmp_path: Path,
) -> None:
    rt = load_runtime()
    ssh = TimedSsh()
    _handle(tmp_path, ssh)
    ssh_limits = [
        limit
        for remote, limit in ssh.limits
        if "mkdir -p" in remote or "ls -1" in remote or "rm -f" in remote
    ]
    assert ssh_limits
    assert max(ssh_limits) <= rt.cycle.copy_budget_seconds
    assert max(ssh_limits) < rt.transport.transfer_timeout_seconds


def test_a_spent_copy_budget_refuses_the_next_copy_and_leaves_the_server_files_alone(
    tmp_path: Path,
) -> None:
    """Each clock reading is 30 s, so the 40 s copy budget is gone after one copy: the run is refused, naming the budget."""
    ticks = iter(range(0, 10_000, 30))
    ssh = TimedSsh()
    with pytest.raises(PublishRefusedError, match="copy budget"):
        _handle(tmp_path, ssh, clock=lambda: float(next(ticks)))
    copies = [r for r, _ in ssh.limits if "mkdir -p" in r]
    assert 1 <= len(copies) < 5
    assert not (tmp_path / "site" / "factory-floor.json").exists()


def test_the_delegation_read_takes_its_limit_from_the_request() -> None:
    seen: list[int] = []

    def runner(
        argv: Sequence[str], stdin: bytes | None, timeout: int
    ) -> subprocess.CompletedProcess[bytes]:
        seen.append(timeout)
        return subprocess.CompletedProcess(list(argv), 1, b"", b"")

    overlay = ModelDelegationReadOverlay(
        ssh_host_env="DELEGATION_HOST",
        compose_project="p",
        postgres_service="db",
        runtime_service="rt",
        db_user="u",
        database="d",
        contract_path="/srv/contract.yaml",
        probe_endpoints=(),
    )
    HandlerDelegationReport(runner).handle(
        ModelDelegationReportRequest(timeout_seconds=15, overlay=overlay),
        environ={"DELEGATION_HOST": "lab"},
    )
    assert seen == [15]
    assert (
        ModelDelegationReportRequest().timeout_seconds == 120
    )  # the session tick keeps its own limit


def test_the_record_step_keeps_its_seconds_when_a_declared_line_is_not_drawn(
    tmp_path: Path,
) -> None:
    rt = load_runtime()
    # the step as it ran, with the seconds it took
    results = [ModelPublishStepResult(name=rt.record_step, status="ok", seconds=23.5)]
    flagged = HandlerFactoryFloorPublish._flag_undrawn(results, rt, ("alpha-plan",), [])
    assert flagged[0].status == "failed"
    assert flagged[0].seconds == 23.5


def test_the_tick_seconds_are_served_beside_the_cycle(tmp_path: Path) -> None:
    ssh = LocalSsh()
    handler = HandlerFactoryFloorPublish(ssh)
    home = _home(tmp_path)
    request = ModelFactoryFloorPublishRequest(
        home=str(home), overlay=_overlay(tmp_path)
    )
    handler.handle(request, environ=PLUGIN)
    handler.record_tick(
        {
            "operator_access": 31.0,
            "floor_publish": 120.5,
            "reports_site": 44.25,
            "run": 200.0,
        }
    )
    served = json.loads((tmp_path / "site" / "publish-status.json").read_text())
    assert served["tick_seconds"] == {
        "operator_access": 31.0,
        "floor_publish": 120.5,
        "reports_site": 44.25,
        "run": 200.0,
    }
    assert served["cycle_seconds"] > 0  # the cycle's own total is still there


def test_no_tick_status_is_written_when_nothing_was_published(tmp_path: Path) -> None:
    ssh = LocalSsh()
    HandlerFactoryFloorPublish(ssh).record_tick({"run": 1.0})
    assert ssh.calls == []
