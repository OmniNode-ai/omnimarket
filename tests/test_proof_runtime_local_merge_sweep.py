# omnimarket/tests/test_proof_runtime_local_merge_sweep.py
"""L1 proof: RuntimeLocal executes node_merge_sweep end-to-end, zero infra."""

from __future__ import annotations

import json
from pathlib import Path

from omnibase_core.enums.enum_workflow_result import EnumWorkflowResult
from omnibase_core.runtime.runtime_local import RuntimeLocal

CONTRACT_PATH = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_merge_sweep/contract.yaml"
)


def test_runtime_local_runs_merge_sweep_with_defaults(tmp_path: Path) -> None:
    """Baseline: RuntimeLocal runs node_merge_sweep with default-constructed payload.

    Proves the substrate wires together but does NOT prove a real workload ran —
    default ModelMergeSweepRequest has prs=[], so classification is trivially empty.
    Task 4 replaces this with a real payload.
    """
    runtime = RuntimeLocal(
        workflow_path=CONTRACT_PATH,
        state_root=tmp_path / "state",
        timeout=30,
    )
    result = runtime.run()

    assert result == EnumWorkflowResult.COMPLETED
    assert runtime.exit_code == 0
    state_file = tmp_path / "state" / "workflow_result.json"
    assert state_file.exists(), f"state file missing at {state_file}"
    data = json.loads(state_file.read_text())
    assert data["result"] == "completed"
    assert data["exit_code"] == 0
    assert data["workflow"].endswith("node_merge_sweep/contract.yaml")
