# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23877: the publish cycle runs node_floor_emissions_publish_effect so the projection the simple floor reads is published each cycle.

One pass folds the three floor emission topics into the floor emissions projection and publishes it on its snapshot topic, which the
floor_projections step reads for the simple floor's readiness rule. The node reads its overlay, publishes the snapshot and exits (65
when the overlay is absent), and it runs only when something calls it.

Golden chain: one publish run calls the node with ``--home <home>`` before the floor step, and the floor step still runs. Error chains: a
node that fails (its overlay absent, exit 65) is recorded failed and the floor still reads; the step dropped from the contract (the
named injected break) makes the golden assertions fail, because the node is then not called in a run.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_factory_floor_publish_effect.handlers import (
    handler_factory_floor_publish as handler_module,
)
from omnimarket.nodes.node_factory_floor_publish_effect.handlers.handler_factory_floor_publish import (
    HandlerFactoryFloorPublish,
    load_runtime,
)
from omnimarket.nodes.node_factory_floor_publish_effect.models.model_factory_floor_publish import (
    ModelFactoryFloorPublishRequest,
)
from tests.nodes.node_factory_floor_publish_effect.test_factory_floor_publish import (
    FLOOR_EMISSIONS,
    FLOOR_SIMPLE,
    PLUGIN,
    Scripted,
    _refresh_request,
)

pytestmark = pytest.mark.unit

RT = load_runtime()


def _module_calls(runner: Scripted) -> list[tuple[str, list[str]]]:
    """Each ``python -m <module> <args>`` the run made, in order."""
    return [
        (c[0][2], c[0][3:]) for c in runner.calls if len(c[0]) > 2 and c[0][1] == "-m"
    ]


def _run(
    tmp_path: Path, code: int = 0
) -> tuple[Scripted, list[tuple[str, str]], ModelFactoryFloorPublishRequest]:
    request = _refresh_request(tmp_path)
    runner = Scripted(
        {"--json": (0, b"[]", b""), FLOOR_EMISSIONS: (code, b"", b"overlay not found")}
    )
    result = HandlerFactoryFloorPublish(runner).handle(request, environ=PLUGIN)
    return runner, [(s.name, s.status) for s in result.steps], request


def test_emissions_golden_the_contract_declares_the_step_before_the_floor_step() -> (
    None
):
    step = next(s for s in RT.steps if s.name == "floor_emissions")
    assert (step.kind, step.module, step.args) == (
        "module",
        FLOOR_EMISSIONS,
        ("--home", "{home}"),
    )
    assert step.concurrent
    assert not step.required
    names = [s.name for s in RT.steps]
    assert names.index("floor_emissions") < names.index("floor_simple")


def test_emissions_golden_one_publish_run_calls_the_node_before_the_floor_step(
    tmp_path: Path,
) -> None:
    runner, steps, request = _run(tmp_path)
    calls = _module_calls(runner)
    modules = [m for m, _ in calls]
    # The concurrent mvp_launch_reads step shares the floor_simple module, so the
    # floor step itself is the last call of that module, not the first.
    assert modules.index(FLOOR_EMISSIONS) < len(modules) - 1 - modules[::-1].index(
        FLOOR_SIMPLE
    )
    assert dict(calls)[FLOOR_EMISSIONS] == ["--home", request.home]
    assert dict(steps)["floor_emissions"] == "ok"
    assert dict(steps)["floor_simple"] == "ok"


def test_emissions_golden_the_step_adds_nothing_to_the_longest_declared_path() -> None:
    group = max(s.timeout_seconds for s in RT.steps if s.concurrent)
    assert RT.declared_longest_path()["input_steps"] == (
        group + sum(s.timeout_seconds for s in RT.steps if not s.concurrent)
    )
    assert next(s for s in RT.steps if s.name == "floor_emissions").timeout_seconds <= (
        group
    )
    assert sum(RT.declared_longest_path().values()) <= RT.cycle.bound_seconds


def test_emissions_error_a_node_that_fails_is_recorded_and_the_floor_still_reads(
    tmp_path: Path,
) -> None:
    runner, steps, _request = _run(tmp_path, code=65)
    assert dict(steps)["floor_emissions"] == "failed"
    assert dict(steps)["floor_simple"] == "ok"
    assert FLOOR_SIMPLE in [m for m, _ in _module_calls(runner)]


def test_emissions_error_control_without_the_step_the_node_is_not_called_in_a_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    without = RT.model_copy(
        update={"steps": tuple(s for s in RT.steps if s.name != "floor_emissions")}
    )
    monkeypatch.setattr(handler_module, "load_runtime", lambda: without)
    runner, steps, _request = _run(tmp_path)
    assert FLOOR_EMISSIONS not in [m for m, _ in _module_calls(runner)]
    assert "floor_emissions" not in dict(steps)
