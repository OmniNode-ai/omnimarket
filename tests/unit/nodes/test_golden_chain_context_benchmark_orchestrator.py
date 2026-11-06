# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: a request through node_context_benchmark_orchestrator reaches a report (OMN-21896).

The contract's terminal event and outputs are the node's declared states: rows, runs and
blocked_lanes. Happy leg: two tasks run in both arms and a row per model and condition
comes back. Error leg: a lane that cannot run is reported blocked with its reason, and its
runs are blocked, never failed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_context_benchmark_orchestrator.handlers.handler_context_benchmark import (
    HandlerContextBenchmark,
)
from omnimarket.nodes.node_context_benchmark_orchestrator.handlers.lane_runners import (
    CommandResult,
)
from omnimarket.nodes.node_context_benchmark_orchestrator.models.model_context_benchmark import (
    EnumBenchmarkLane,
    EnumRunOutcome,
    ModelBenchmarkLaneSpec,
    ModelBenchmarkTask,
    ModelContextBenchmarkRequest,
)

pytestmark = pytest.mark.unit

_CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_context_benchmark_orchestrator/contract.yaml"
)


def _request(tmp_path: Path, lane: EnumBenchmarkLane) -> ModelContextBenchmarkRequest:
    source = tmp_path / "source"
    source.mkdir()
    (source / "CLAUDE.md").write_text("c\n")
    (source / "AGENTS.md").write_text("a\n")
    return ModelContextBenchmarkRequest(
        source_dir=str(source),
        tasks=(ModelBenchmarkTask(task_id="t1", prompt="p", check_argv=("check",)),),
        lanes=(ModelBenchmarkLaneSpec(lane=lane, model_id="m"),),
        scratch_dir=str(tmp_path / "scratch"),
        host="h-test",
    )


def test_context_benchmark_golden_chain_declared_states_and_terminal_event() -> None:
    contract = yaml.safe_load(_CONTRACT.read_text())
    assert (
        contract["terminal_event"]
        == "onex.evt.omnimarket.context-benchmark-completed.v1"
    )
    assert set(contract["outputs"]) == {"rows", "runs", "blocked_lanes"}


def test_context_benchmark_golden_chain_codex_lane_reaches_a_report(
    tmp_path: Path,
) -> None:
    events = "\n".join(
        json.dumps(e)
        for e in (
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 10,
                    "cached_input_tokens": 4,
                    "output_tokens": 2,
                },
            },
        )
    )

    def command(argv, cwd, timeout, stdin, env):
        return CommandResult(0, events if argv[0] == "codex" else "", "")

    result = HandlerContextBenchmark(command_fn=command).handle(
        _request(tmp_path, EnumBenchmarkLane.CODEX_HEADLESS)
    )
    assert [r.condition.value for r in result.rows] == ["on", "off"]
    assert {r.outcome for r in result.runs} == {EnumRunOutcome.PASS}
    assert result.rows[0].underpowered


def test_context_benchmark_golden_chain_error_leg_blocked_lane_is_reported(
    tmp_path: Path,
) -> None:
    def refuse(argv, cwd, timeout, stdin, env):
        return CommandResult(
            3,
            json.dumps(
                {
                    "decided_by": "incumbent_refused",
                    "reason": "credential_not_registered",
                }
            ),
            "",
        )

    request = _request(tmp_path, EnumBenchmarkLane.JEV_TYPED_DECISION)
    from omnimarket.nodes.node_context_benchmark_orchestrator.models.model_context_benchmark import (
        ModelJevQuestion,
    )

    task = request.tasks[0].model_copy(
        update={
            "jev_question": ModelJevQuestion(
                instructions="q", options={"a": "x", "b": "y"}, expected_answer="a"
            )
        }
    )
    result = HandlerContextBenchmark(command_fn=refuse).handle(
        request.model_copy(update={"tasks": (task,)})
    )
    assert {r.outcome for r in result.runs} == {EnumRunOutcome.BLOCKED}
    assert "credential-absent" in result.blocked_lanes[0]
