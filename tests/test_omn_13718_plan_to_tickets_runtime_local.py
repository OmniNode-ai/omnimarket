# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-13718 regression: RuntimeLocal must resolve input_model for node_plan_to_tickets.

Before the fix, RuntimeLocal raised:
  "could not resolve an initial-payload model for contract 'node_plan_to_tickets'.
   Declare a top-level 'input_model' ..."
because the contract.yaml had no top-level `input_model` field, only `handler.input_model`.
The event-driven execution path requires the top-level declaration.

Fix: contract.yaml gains `input_model: omnimarket.events.design_to_plan.ModelPlanToTicketsStartCommand`
at the top level, plus an `event_model` block on the handler_routing entry.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from omnibase_core.enums.enum_workflow_result import EnumWorkflowResult

from tests.runtime_local_compat import RuntimeLocal

CONTRACT_PATH = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_plan_to_tickets/contract.yaml"
)

_SIMPLE_PLAN = """\
# Widget Epic

## Task 1: Build the widget

Implement the core widget logic.

## Task 2: Write tests

Dependencies: Task 1

Write unit and integration tests.
"""


@pytest.mark.unit
def test_runtime_local_resolves_input_model(tmp_path: Path) -> None:
    """RuntimeLocal must not raise 'could not resolve initial-payload model'.

    This is the reproducing test for OMN-13718. Before the fix the contract
    lacked a top-level `input_model`, causing RuntimeLocal to refuse dispatch
    with an error on every `onex skill plan_to_tickets` invocation.
    """
    plan_file = tmp_path / "plan.md"
    plan_file.write_text(_SIMPLE_PLAN, encoding="utf-8")

    # Build the input fixture that the skill CLI would produce from --plan-path
    fixture_path = tmp_path / "input.json"
    fixture_path.write_text(
        json.dumps({"plan_path": str(plan_file), "dry_run": True}),
        encoding="utf-8",
    )

    state_root = tmp_path / "state"
    runtime = RuntimeLocal(
        workflow_path=CONTRACT_PATH,
        state_root=state_root,
        input_path=fixture_path,
        timeout=30,
    )
    result = runtime.run()

    # Before the fix this was EnumWorkflowResult.FAILED with the error message
    # "could not resolve an initial-payload model for contract 'node_plan_to_tickets'"
    assert result == EnumWorkflowResult.COMPLETED, (
        f"Expected COMPLETED, got {result}. "
        f"If FAILED, check state_root for the captured error log."
    )
    assert runtime.exit_code == 0

    state_file = state_root / "workflow_result.json"
    assert state_file.exists(), f"state file missing at {state_file}"
    data = json.loads(state_file.read_text())
    assert data["result"] == "completed"
    assert data["exit_code"] == 0
    assert data["workflow"].endswith("node_plan_to_tickets/contract.yaml")

    # Confirm the handler actually ran and parsed the plan (not vacuous)
    terminal_payload = data.get("terminal_payload") or {}
    assert terminal_payload.get("status") == "parsed", (
        f"Expected status='parsed', got: {terminal_payload}"
    )
    assert terminal_payload.get("entry_count", 0) == 2, (
        f"Expected 2 entries from _SIMPLE_PLAN, got: {terminal_payload}"
    )
