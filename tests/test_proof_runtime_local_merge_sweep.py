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


def test_runtime_local_runs_merge_sweep_with_real_payload(tmp_path: Path) -> None:
    """OMN-8939 L1A proof: RuntimeLocal + EventBusInmemory + ServiceStateDisk
    execute node_merge_sweep against a non-empty ModelMergeSweepRequest
    (2 fixture PRs covering mergeable + blocked cases) with no OmniNode infra.
    """
    fixture_path = (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "merge_sweep_input_sample.json"
    )
    assert fixture_path.exists(), f"fixture missing at {fixture_path}"

    state_root = tmp_path / "state"
    runtime = RuntimeLocal(
        workflow_path=CONTRACT_PATH,
        state_root=state_root,
        input_path=fixture_path,
        timeout=30,
    )
    result = runtime.run()

    assert result == EnumWorkflowResult.COMPLETED, f"Runtime did not complete: {result}"
    assert runtime.exit_code == 0

    state_file = state_root / "workflow_result.json"
    assert state_file.exists()
    state = json.loads(state_file.read_text())
    # Required-field assertions only — robust to future runtime metadata additions.
    # EnumWorkflowResult.COMPLETED.value is the lowercase string "completed".
    assert state["result"] == "completed"
    assert state["exit_code"] == 0
    assert state["workflow"].endswith("node_merge_sweep/contract.yaml")


def test_runtime_local_publishes_terminal_event_to_bus(tmp_path: Path) -> None:
    """OMN-8940 L1B proof: the terminal topic transits EventBusInmemory during
    an L1 run on a synchronous-return handler (NodeMergeSweep).

    Depends on the runtime behavior decision in Task 5: RuntimeLocal publishes a
    runtime-synthesized terminal event after successful sync-return classification.
    Before that decision, NodeMergeSweep's sync return bypassed the bus entirely.
    """
    fixture_path = (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "merge_sweep_input_sample.json"
    )
    runtime = RuntimeLocal(
        workflow_path=CONTRACT_PATH,
        state_root=tmp_path / "state",
        input_path=fixture_path,
        timeout=30,
    )
    runtime.run()

    terminal_count = runtime._events_received.get("(terminal)", 0)
    assert terminal_count >= 1, (
        f"No terminal events recorded on the bus; _events_received={runtime._events_received}"
    )
