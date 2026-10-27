# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The packaged gate returns admission decisions through the local runtime."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from omnibase_core.enums.enum_workflow_result import EnumWorkflowResult

from omnimarket.handlers.rules_wip_gate import load_wip_gate_contract
from omnimarket.models.wip_gate import ModelWipGateDecision
from tests.runtime_local_compat import RuntimeLocal

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("open_count", "enforce", "readable", "outcome", "reason"),
    [
        (4, True, True, "admit", "under_limit"),
        (5, True, True, "refuse", "at_limit"),
        (5, False, True, "warn", "at_limit"),
        (0, True, False, "refuse", "missing_reading"),
        (0, False, False, "warn", "missing_reading"),
    ],
)
def test_gate_decision_through_runtime_local(
    tmp_path: Path,
    open_count: int,
    enforce: bool,
    readable: bool,
    outcome: str,
    reason: str,
) -> None:
    now = "2026-10-27T12:00:00Z"
    state = {
        "last_tick": now,
        "prs": {
            f"example#{i + 1}": {
                "facts": {
                    "repo": "example",
                    "number": i + 1,
                    "state": "OPEN",
                    "draft": i == 0,
                }
            }
            for i in range(open_count)
        },
    }
    input_path = tmp_path / "input.json"
    input_path.write_text(
        json.dumps(
            {
                "repo": "example",
                "dispatch_id": "dispatch-1",
                "now": now,
                "state": state if readable else None,
                "policy": {
                    "enforce_ruling": "enforcing" if enforce else "",
                    "dispatchable_repos": ["example"],
                    "exempt_roles": [],
                    "limits": {
                        "example": {
                            "limit": 5,
                            "merges_per_hour": 1.5,
                            "method": "supplied measurement",
                            "evidence": "supplied evidence",
                        }
                    },
                },
                "config": load_wip_gate_contract()[1].model_dump(),
            }
        )
    )
    state_root = tmp_path / "state"
    contract = (
        Path(__file__).resolve().parents[3]
        / "src/omnimarket/nodes/node_wip_gate_reducer/contract.yaml"
    )
    runtime = RuntimeLocal(
        workflow_path=contract,
        state_root=state_root,
        input_path=input_path,
        timeout=30,
    )
    assert runtime.run() == EnumWorkflowResult.COMPLETED
    written = json.loads((state_root / "workflow_result.json").read_text())
    terminal = written.get("terminal_payload") or written["handler_result"]
    decision = ModelWipGateDecision.model_validate(terminal)
    assert decision.outcome == outcome
    assert decision.reason == reason
    assert (
        decision.fsm_state
        == {"admit": "admitted", "refuse": "refused", "warn": "warned"}[outcome]
    )
    assert decision.repo == "example"
    assert decision.open == (open_count if readable else None)
    assert decision.drafts == (int(open_count > 0) if readable else None)
