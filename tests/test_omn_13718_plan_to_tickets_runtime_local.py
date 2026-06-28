# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-13718 proof: RuntimeLocal resolves input_model for node_plan_to_tickets.

Before the fix: RuntimeLocal emitted
    "event-driven workflow could not resolve an initial-payload model for
    contract 'node_plan_to_tickets'. Declare a top-level 'input_model'..."
and exited with result=failed.

After the fix:
  - contract.yaml gains a top-level `input_model` and an `event_model` block
    on the handler_routing entry, so RuntimeLocal can seed the initial command.
  - result=completed, exit_code=0, terminal_payload.status=parsed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from omnibase_core.enums.enum_workflow_result import EnumWorkflowResult

from tests.runtime_local_compat import RuntimeLocal

_CONTRACT_PATH = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_plan_to_tickets/contract.yaml"
)

_SIMPLE_PLAN = """\
# OMN-13718 Proof Plan

## Task 1: First task

Implement the first thing.

## Task 2: Second task

Dependencies: Task 1

Implement the second thing.
"""


@pytest.mark.unit
def test_runtime_local_resolves_input_model(tmp_path: Path) -> None:
    """RuntimeLocal must resolve input_model and complete — not refuse with model-resolution error.

    Reproduces the OMN-13718 failure:
        RuntimeLocal: event-driven workflow could not resolve an initial-payload
        model for contract 'node_plan_to_tickets'. Declare a top-level 'input_model'...

    After fix: result=completed, exit_code=0, terminal_payload.status=parsed.
    """
    plan_file = tmp_path / "plan.md"
    plan_file.write_text(_SIMPLE_PLAN, encoding="utf-8")

    # Build a minimal input JSON that matches ModelPlanToTicketsStartCommand.
    input_data = {"plan_path": str(plan_file), "dry_run": True}
    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(input_data), encoding="utf-8")

    runtime = RuntimeLocal(
        workflow_path=_CONTRACT_PATH,
        state_root=tmp_path / "state",
        input_path=input_path,
        timeout=30,
    )
    result = runtime.run()

    assert result == EnumWorkflowResult.COMPLETED, (
        f"Runtime did not complete: {result}. "
        "Before the fix this would be FAILED with 'could not resolve initial-payload model'."
    )
    assert runtime.exit_code == 0

    state_file = tmp_path / "state" / "workflow_result.json"
    assert state_file.exists(), f"State file missing: {state_file}"
    data = json.loads(state_file.read_text(encoding="utf-8"))

    assert data["result"] == "completed", f"Unexpected result: {data['result']}"
    assert data["exit_code"] == 0

    # terminal_payload must be present and reflect a real parse run.
    terminal = data.get("terminal_payload")
    assert terminal is not None, "terminal_payload missing from workflow_result"
    assert terminal.get("status") == "parsed", (
        f"Expected status=parsed, got: {terminal.get('status')}"
    )
    entry_count = terminal.get("entry_count", 0)
    assert entry_count == 2, f"Expected 2 entries, got {entry_count}"
