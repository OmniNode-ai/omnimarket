# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Usage survives the packaged node's actual input/terminal serialization."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from omnibase_core.enums.enum_workflow_result import EnumWorkflowResult

from tests.runtime_local_compat import RuntimeLocal

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("engine", ["claude", "codex"])
def test_usage_through_runtime_local(tmp_path: Path, engine: str) -> None:
    fixtures = Path(__file__).parent / "fixtures"
    events = json.loads((fixtures / f"{engine}_recorded_events.json").read_text())[
        "events"
    ]
    expected = json.loads((fixtures / "recorded_expected.json").read_text())[engine]
    input_path = tmp_path / "input.json"
    input_path.write_text(
        json.dumps(
            {
                "engine_exit_code": 0,
                "engine": engine,
                "engine_output": events,
                "brief": "Contract: work-42\nAttempt: attempt-2\n",
            }
        )
    )
    state = tmp_path / "state"
    contract = (
        Path(__file__).resolve().parents[3]
        / "src/omnimarket/nodes/node_remote_lane_close_compute/contract.yaml"
    )
    runtime = RuntimeLocal(
        workflow_path=contract, state_root=state, input_path=input_path, timeout=30
    )
    assert runtime.run() == EnumWorkflowResult.COMPLETED
    written = json.loads((state / "workflow_result.json").read_text())
    terminal = written.get("terminal_payload") or written["handler_result"]
    assert terminal["usage"] == {
        **expected,
        "contract_id": "work-42",
        "attempt_id": "attempt-2",
    }
    assert terminal["ledger_cells"] == {
        "contract_id": "work-42",
        "attempt_id": "attempt-2",
    }
