"""The 14 retired context and compaction nodes stay removed."""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

RETIRED_NODES = (
    "node_context_adr_select_compute",
    "node_context_compaction_receipts_fold_compute",
    "node_context_doc_section_select_compute",
    "node_context_experiment_compute",
    "node_context_exploration_policy_compute",
    "node_context_gate_cascade_compute",
    "node_context_invariants_select_compute",
    "node_context_role_adoption_verdict_compute",
    "node_context_round_one_eval_compute",
    "node_context_source_git_effect",
    "node_delegation_context_pack_budget_compute",
    "node_design_plan_context_compute",
    "node_report_format_compute",
    "node_checkpoint_capture_effect",
)


@pytest.mark.unit
@pytest.mark.parametrize("name", RETIRED_NODES)
def test_retired_node_directory_is_absent(name: str) -> None:
    assert not (ROOT / "src" / "omnimarket" / "nodes" / name).exists()
    assert not (ROOT / "tests" / "nodes" / name).exists()


@pytest.mark.unit
@pytest.mark.parametrize("name", RETIRED_NODES)
def test_retired_node_entry_point_is_absent(name: str) -> None:
    pyproject = (ROOT / "pyproject.toml").read_text()
    assert not any(line.startswith(f"{name} = ") for line in pyproject.splitlines())
