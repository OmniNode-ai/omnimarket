# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23122: the publish cycle runs node_join_j16_effect before the simple floor reads its receipts.

Join J16 (piece 16, context, memory and evals, to piece 17, the floor) was tested and never run: the served floor of 2026-11-08
08:16Z read J16 not_run with no last run. The node writes the join's two chains to the emission journal, and it runs only when
something calls it.

Golden chain: one publish run calls the node with ``run`` before the floor step, and the floor step still runs. Error chains: a
node that fails (a deployment fact missing, exit 3) is recorded failed and the floor still reads; the step dropped from the
contract (the named injected break) makes the golden assertions fail, because the node is then not called in a run.
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
    JOIN_J16,
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


def _run(tmp_path: Path, code: int = 0) -> tuple[Scripted, list[tuple[str, str]]]:
    runner = Scripted(
        {"--json": (0, b"[]", b""), JOIN_J16: (code, b"", b"INDETERMINATE: x")}
    )
    result = HandlerFactoryFloorPublish(runner).handle(
        _refresh_request(tmp_path), environ=PLUGIN
    )
    return runner, [(s.name, s.status) for s in result.steps]


def test_j16_golden_the_contract_declares_the_step_before_the_floor_step() -> None:
    step = next(s for s in RT.steps if s.name == "join_j16")
    assert (step.kind, step.module, step.args) == ("module", JOIN_J16, ("run",))
    assert step.concurrent
    assert not step.required
    names = [s.name for s in RT.steps]
    assert names.index("join_j16") < names.index("floor_simple")


def test_j16_golden_one_publish_run_calls_the_node_before_the_floor_step(
    tmp_path: Path,
) -> None:
    runner, steps = _run(tmp_path)
    calls = _module_calls(runner)
    modules = [m for m, _ in calls]
    assert modules.index(JOIN_J16) < modules.index(FLOOR_SIMPLE)
    assert dict(calls)[JOIN_J16] == ["run"]
    assert dict(steps)["join_j16"] == "ok"
    assert dict(steps)["floor_simple"] == "ok"


def test_j16_golden_the_step_adds_nothing_to_the_longest_declared_path() -> None:
    group = max(s.timeout_seconds for s in RT.steps if s.concurrent)
    assert RT.declared_longest_path()["input_steps"] == (
        group + sum(s.timeout_seconds for s in RT.steps if not s.concurrent)
    )
    assert next(s for s in RT.steps if s.name == "join_j16").timeout_seconds <= group
    assert sum(RT.declared_longest_path().values()) <= RT.cycle.bound_seconds


def test_j16_error_a_node_that_fails_is_recorded_and_the_floor_still_reads(
    tmp_path: Path,
) -> None:
    runner, steps = _run(tmp_path, code=3)
    assert dict(steps)["join_j16"] == "failed"
    assert dict(steps)["floor_simple"] == "ok"
    assert FLOOR_SIMPLE in [m for m, _ in _module_calls(runner)]


def test_j16_error_control_without_the_step_the_node_is_not_called_in_a_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    without = RT.model_copy(
        update={"steps": tuple(s for s in RT.steps if s.name != "join_j16")}
    )
    monkeypatch.setattr(handler_module, "load_runtime", lambda: without)
    runner, steps = _run(tmp_path)
    assert JOIN_J16 not in [m for m, _ in _module_calls(runner)]
    assert "join_j16" not in dict(steps)
