# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24281: the publish cycle runs the floor projections' publishers each cycle.

Nothing ran node_floor_owner_map_publish_effect or node_work_state_projection_effect, so the owner_map and
work_state projections node_floor_projection_read_effect reads were never published. Each publisher now runs as
its own concurrent input step, ahead of the floor_projections step that reads what they published.

Golden chain: one publish run calls each publisher with the args its contract step declares, before the floor
step, and each step reads ok. Error chains: a publisher that fails (exit 65) is recorded failed and the floor
still reads; a step dropped from the contract (the named injected break) makes the golden assertions fail,
because its node is then not called in a run.
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
from tests.nodes.node_factory_floor_publish_effect.limits_support import TEST_LIMITS
from tests.nodes.node_factory_floor_publish_effect.test_factory_floor_publish import (
    FLOOR_SIMPLE,
    PLUGIN,
    Scripted,
    _refresh_request,
)

pytestmark = pytest.mark.unit

RT = load_runtime().with_limits(TEST_LIMITS)

OWNER_MAP = "omnimarket.nodes.node_floor_owner_map_publish_effect"
WORK_STATE = "omnimarket.nodes.node_work_state_projection_effect"

STEPS: tuple[tuple[str, str], ...] = (
    ("floor_owner_map", OWNER_MAP),
    ("floor_work_state", WORK_STATE),
)


def _module_calls(runner: Scripted) -> list[tuple[str, list[str]]]:
    """Each ``python -m <module> <args>`` the run made, in order."""
    return [
        (c[0][2], c[0][3:]) for c in runner.calls if len(c[0]) > 2 and c[0][1] == "-m"
    ]


def _run(
    tmp_path: Path, answers: dict[str, tuple[int, bytes, bytes]] | None = None
) -> tuple[Scripted, list[tuple[str, str]], ModelFactoryFloorPublishRequest]:
    request = _refresh_request(tmp_path)
    runner = Scripted({"--json": (0, b"[]", b""), **(answers or {})})
    result = HandlerFactoryFloorPublish(runner).handle(request, environ=PLUGIN)
    return runner, [(s.name, s.status) for s in result.steps], request


@pytest.mark.parametrize(("step_name", "module"), STEPS)
def test_projection_golden_the_contract_declares_the_step_before_the_projection_and_floor_steps(
    step_name: str, module: str
) -> None:
    step = next(s for s in RT.steps if s.name == step_name)
    assert (step.kind, step.module) == ("module", module)
    assert step.concurrent
    assert not step.required
    names = [s.name for s in RT.steps]
    assert names.index(step_name) < names.index("floor_projections")
    assert names.index(step_name) < names.index("floor_simple")


@pytest.mark.parametrize(("step_name", "module"), STEPS)
def test_projection_golden_one_publish_run_calls_the_node_before_the_floor_step(
    tmp_path: Path, step_name: str, module: str
) -> None:
    runner, steps, request = _run(tmp_path)
    calls = _module_calls(runner)
    modules = [m for m, _ in calls]
    # The concurrent mvp_launch_reads step shares the floor_simple module, so the
    # floor step itself is the last call of that module, not the first.
    assert modules.index(module) < len(modules) - 1 - modules[::-1].index(FLOOR_SIMPLE)
    step = next(s for s in RT.steps if s.name == step_name)
    assert dict(calls)[module] == [a.replace("{home}", request.home) for a in step.args]
    assert dict(steps)[step_name] == "ok"
    assert dict(steps)["floor_simple"] == "ok"


@pytest.mark.parametrize(("step_name", "module"), STEPS)
def test_projection_golden_the_step_adds_nothing_to_the_longest_declared_path(
    step_name: str, module: str
) -> None:
    group = max(s.timeout_seconds for s in RT.steps if s.concurrent)
    assert RT.declared_longest_path()["input_steps"] == (
        group + sum(s.timeout_seconds for s in RT.steps if not s.concurrent)
    )
    assert next(s for s in RT.steps if s.name == step_name).timeout_seconds <= (group)
    assert sum(RT.declared_longest_path().values()) <= RT.cycle.bound_seconds


@pytest.mark.parametrize(("step_name", "module"), STEPS)
def test_projection_error_a_node_that_fails_is_recorded_and_the_floor_still_reads(
    tmp_path: Path, step_name: str, module: str
) -> None:
    runner, steps, _request = _run(tmp_path, {module: (65, b"", b"overlay not found")})
    assert dict(steps)[step_name] == "failed"
    assert dict(steps)["floor_simple"] == "ok"
    assert FLOOR_SIMPLE in [m for m, _ in _module_calls(runner)]


@pytest.mark.parametrize(("step_name", "module"), STEPS)
def test_projection_error_control_without_the_step_the_node_is_not_called_in_a_run(
    tmp_path: Path,
    step_name: str,
    module: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    without = RT.model_copy(
        update={"steps": tuple(s for s in RT.steps if s.name != step_name)}
    )
    monkeypatch.setattr(handler_module, "load_runtime", lambda: without)
    runner, steps, _request = _run(tmp_path)
    assert module not in [m for m, _ in _module_calls(runner)]
    assert step_name not in dict(steps)
