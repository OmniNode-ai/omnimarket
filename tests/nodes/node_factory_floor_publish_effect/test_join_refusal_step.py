# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22864: the publish cycle runs node_join_refusal_effect before the simple floor reads its receipts.

The node writes the downstream piece's typed refusal of every real upstream failure, and it runs only when something calls
it. Measured on the served board of 2026-11-08 02:44Z: J2 (28), J3 (1), J4 (8) and J5 (22) read
``downstream_missing_typed_failure`` on the chains row, and no unit, timer or publish step ran the node.

Golden chain: one publish run calls the node with ``run`` before the floor step, and the floor step still runs. Error chains:
a node that fails (a deployment fact missing, exit 3) is recorded failed and the floor still reads; the step dropped from the contract (the named injected break) makes the golden assertions
fail, because no refusal is then written in a run.
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
from tests.nodes.node_factory_floor_publish_effect.test_factory_floor_publish import (
    FLOOR_SIMPLE,
    JOIN_REFUSAL,
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


def _modules(runner: Scripted) -> list[str]:
    return [m for m, _ in _module_calls(runner)]


def _run(tmp_path: Path, code: int = 0) -> tuple[Scripted, list[tuple[str, str]]]:
    runner = Scripted(
        {"--json": (0, b"[]", b""), JOIN_REFUSAL: (code, b"", b"INDETERMINATE: x")}
    )
    result = HandlerFactoryFloorPublish(runner).handle(
        _refresh_request(tmp_path), environ=PLUGIN
    )
    return runner, [(s.name, s.status) for s in result.steps]


def test_golden_the_contract_declares_the_refusal_step_before_the_floor_step() -> None:
    steps = {s.name: s for s in RT.steps}
    step = steps["join_refusal"]
    assert (step.kind, step.module, step.args) == ("module", JOIN_REFUSAL, ("run",))
    assert step.concurrent
    assert not step.required
    names = [s.name for s in RT.steps]
    assert names.index("join_refusal") < names.index("floor_simple")


def test_golden_one_publish_run_calls_the_node_before_the_floor_step(
    tmp_path: Path,
) -> None:
    runner, steps = _run(tmp_path)
    modules = _modules(runner)
    assert modules.index(JOIN_REFUSAL) < modules.index(FLOOR_SIMPLE)
    assert dict(_module_calls(runner))[JOIN_REFUSAL] == ["run"]
    assert dict(steps)["join_refusal"] == "ok"


def test_golden_the_step_adds_nothing_to_the_longest_declared_path() -> None:
    group = max(s.timeout_seconds for s in RT.steps if s.concurrent)
    assert RT.declared_longest_path()["input_steps"] == (
        group + sum(s.timeout_seconds for s in RT.steps if not s.concurrent)
    )
    assert (
        next(s for s in RT.steps if s.name == "join_refusal").timeout_seconds <= group
    )
    assert sum(RT.declared_longest_path().values()) <= RT.cycle.bound_seconds


def test_error_a_node_that_fails_is_recorded_and_the_floor_still_reads(
    tmp_path: Path,
) -> None:
    runner, steps = _run(tmp_path, code=3)
    assert dict(steps)["join_refusal"] == "failed"
    floor_runs = [a for m, a in _module_calls(runner) if m == FLOOR_SIMPLE]
    assert any("--defer-canary" in a for a in floor_runs)
    assert dict(steps)["floor_simple"] == "ok"


def test_error_control_without_the_step_no_refusal_is_written_in_a_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    without = RT.model_copy(
        update={"steps": tuple(s for s in RT.steps if s.name != "join_refusal")}
    )
    monkeypatch.setattr(handler_module, "load_runtime", lambda: without)
    runner, steps = _run(tmp_path)
    assert JOIN_REFUSAL not in _modules(runner)
    assert "join_refusal" not in dict(steps)
