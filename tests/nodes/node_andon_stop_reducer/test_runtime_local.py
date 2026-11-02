# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The packaged reducer folds a stop event through the local runtime."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from omnibase_core.enums.enum_workflow_result import EnumWorkflowResult

from omnimarket.models.stop_gate import ModelStopFoldResult
from tests.runtime_local_compat import RuntimeLocal

pytestmark = pytest.mark.unit

STOPPED_AT = "2026-11-01T10:00:00Z"


@pytest.mark.parametrize(
    ("kind", "role", "fixture_state", "status", "state"),
    [
        ("lease_granted", "swarm_lane", "open", "applied", "swarming"),
        ("declared_false", "operator", "open", "applied", "declared_false"),
        ("lease_granted", "readback", "open", "refused", "open"),
        ("lease_granted", "swarm_lane", "cleared", "refused", "cleared"),
    ],
)
def test_fold_through_runtime_local(
    tmp_path: Path, kind: str, role: str, fixture_state: str, status: str, state: str
) -> None:
    input_path = tmp_path / "input.json"
    input_path.write_text(
        json.dumps(
            {
                "stop": {
                    "id": "stop-1",
                    "cls": "engine-policy-breach",
                    "scope": {"kind": "engine", "key": "example-engine"},
                    "state": fixture_state,
                    "stopped_at": STOPPED_AT,
                },
                "event": {
                    "stop_id": "stop-1",
                    "kind": kind,
                    "role": role,
                    "at": "2026-11-01T10:05:00Z",
                    "lane_id": "swarm-lane-1",
                },
            }
        )
    )
    state_root = tmp_path / "state"
    contract = (
        Path(__file__).resolve().parents[3]
        / "src/omnimarket/nodes/node_andon_stop_reducer/contract.yaml"
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
    result = ModelStopFoldResult.model_validate(terminal)
    assert result.status.value == status
    assert result.stop.state == state
