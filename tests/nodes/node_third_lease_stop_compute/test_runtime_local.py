# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The packaged node decides through the local runtime."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from omnibase_core.enums.enum_workflow_result import EnumWorkflowResult

from omnimarket.nodes.node_third_lease_stop_compute.models.model_third_lease_stop import (
    EnumThirdLeaseStopDecision,
    ModelThirdLeaseStopResult,
)
from tests.runtime_local_compat import RuntimeLocal

pytestmark = pytest.mark.unit

CAUSE = "cause:Example/repo:aaaaaaaaaaaa"
CONFIG = {
    "stop_class_id": "cause-leased-third-time",
    "grammar": {
        "lease_row_type": "CLAIM",
        "cause_cell": "cause",
        "lane_cell": "lane",
        "lease_lane_prefixes": ["landing-cause-", "landing-"],
        "record_row_type": "STATUS",
        "record_cell": "go-and-see",
    },
    "replay_window_hours": 96,
    "lease_count": 3,
    "mode": "report-only",
    "row_source": "memory",
}
LEASES = [
    f"2026-11-01T10:00:00Z | CLAIM | lane=landing-cause-L1 | cause={CAUSE}",
    f"2026-11-01T10:01:00Z | CLAIM | lane=landing-L1 | cause={CAUSE}",
    f"2026-11-01T11:00:00Z | CLAIM | lane=landing-cause-L2 | cause={CAUSE}",
]
RECORD = f"2026-11-01T11:30:00Z | STATUS | lane=reader | go-and-see={CAUSE}"


@pytest.mark.parametrize(
    ("rows", "decision"),
    [
        (LEASES, EnumThirdLeaseStopDecision.STOP),
        ([*LEASES, RECORD], EnumThirdLeaseStopDecision.ADMIT),
    ],
)
def test_third_lease_stop_through_runtime_local(
    tmp_path: Path, rows: list[str], decision: EnumThirdLeaseStopDecision
) -> None:
    input_path = tmp_path / "input.json"
    input_path.write_text(
        json.dumps(
            {
                "rows": rows,
                "now": "2026-11-01T12:00:00Z",
                "cause": CAUSE,
                "config": CONFIG,
            }
        )
    )
    state_root = tmp_path / "state"
    contract = (
        Path(__file__).resolve().parents[3]
        / "src/omnimarket/nodes/node_third_lease_stop_compute/contract.yaml"
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
    result = ModelThirdLeaseStopResult.model_validate(terminal)
    (only,) = result.decisions
    assert only.decision is decision
    assert not result.blocks


def test_third_lease_stop_replay_through_runtime_local(tmp_path: Path) -> None:
    """The recorded four days through the packaged runtime: the count line the operator reads."""
    fixture = (
        Path(__file__).resolve().parents[2]
        / "fixtures/third_lease_stop/cause_lease_rows.txt"
    )
    input_path = tmp_path / "input.json"
    input_path.write_text(
        json.dumps(
            {
                "rows": fixture.read_text(encoding="utf-8").splitlines(),
                "now": "2026-11-02T02:35:00Z",
                "config": CONFIG,
            }
        )
    )
    contract = (
        Path(__file__).resolve().parents[3]
        / "src/omnimarket/nodes/node_third_lease_stop_compute/contract.yaml"
    )
    runtime = RuntimeLocal(
        workflow_path=contract,
        state_root=tmp_path / "state",
        input_path=input_path,
        timeout=60,
    )
    assert runtime.run() == EnumWorkflowResult.COMPLETED
    written = json.loads((tmp_path / "state/workflow_result.json").read_text())
    terminal = written.get("terminal_payload") or written["handler_result"]
    result = ModelThirdLeaseStopResult.model_validate(terminal)
    assert result.line == (
        "third_lease_stop mode=report-only lease_count=3 window_hours=96 causes=103 "
        "would_stop=295 gate_stopped=53 leases=842 records=0 rows_unreadable=0"
    )
    assert len(result.would_stop) == 295
    assert not result.blocks
