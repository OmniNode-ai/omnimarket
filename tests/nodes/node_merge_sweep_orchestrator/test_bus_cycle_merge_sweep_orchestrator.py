# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A merge sweep requested over the bus, as a caller requests it (OMN-20676).

The caller's entry is ``onex node node_merge_sweep_orchestrator --input <request.json>``: the
canonical local runtime loads the packaged contract, publishes the request on the contract's
command topic, the handler the contract routes answers it, and the result lands on the declared
completed topic. These tests run that runtime over the recorded sweep with dispatch off, so no
lane is started.
"""

from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path

from omnibase_core.enums.enum_workflow_result import EnumWorkflowResult
from omnibase_core.runtime.runtime_local import RuntimeLocal

from omnimarket.nodes.node_merge_sweep_orchestrator.handlers import (
    HandlerMergeSweepRun,
)
from omnimarket.nodes.node_merge_sweep_orchestrator.models import (
    ModelMergeSweepRunRequest,
    ModelMergeSweepRunResult,
)

from ..node_merge_sweep_reading_compute.sweep_scenarios import load_recorded
from .support import run_request

NAME = "node_merge_sweep_orchestrator"
CONTRACT = Path(str(files(f"omnimarket.nodes.{NAME}").joinpath("contract.yaml")))
RECORDED = load_recorded()


def _over_the_bus(
    request: ModelMergeSweepRunRequest, tmp_path: Path
) -> tuple[EnumWorkflowResult, ModelMergeSweepRunResult]:
    """Run the packaged contract on the local runtime with the request as its input."""
    input_path = tmp_path / "request.json"
    input_path.write_text(request.model_dump_json())
    runtime = RuntimeLocal(
        workflow_path=CONTRACT,
        state_root=tmp_path / "state",
        input_path=input_path,
        timeout=60,
    )
    outcome = runtime.run()
    state = json.loads((tmp_path / "state" / "workflow_result.json").read_text())
    assert state["terminal_payload"] is not None, state
    return outcome, ModelMergeSweepRunResult.model_validate(state["terminal_payload"])


def test_a_sweep_requested_on_the_command_topic_completes_with_its_plan(
    tmp_path: Path,
) -> None:
    spec = RECORDED["scenarios"][0]
    request = run_request(spec, RECORDED["base"], tmp_path / "files", dispatch=False)
    outcome, result = _over_the_bus(request, tmp_path)
    assert outcome == EnumWorkflowResult.COMPLETED
    assert result.ok, result.why
    assert result.outcomes == []
    assert result.plan is not None
    assert result.plan.lanes
    assert result.plan == HandlerMergeSweepRun().handle(request).plan


def test_a_refused_sweep_answers_on_the_bus_and_starts_no_lane(
    tmp_path: Path,
) -> None:
    spec = RECORDED["scenarios"][0]
    request = run_request(spec, RECORDED["base"], tmp_path / "files", dispatch=False)
    Path(request.state_path).unlink()
    _, result = _over_the_bus(request, tmp_path)
    assert result.ok is False
    assert result.why
    assert result.plan is None
    assert result.outcomes == []
