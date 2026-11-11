# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-23538: the duration record lists the test files that were unrecorded."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

RECORD = Path(__file__).resolve().parents[2] / "config" / "test_file_durations.json"

RECORDED_BY_OMN_23538 = (
    "tests/chain/test_watcher_facts_snapshot_seam.py",
    "tests/ci/test_repo_evidence_preflight_startup_failure_omn23087.py",
    "tests/nodes/node_delegated_code_edit_orchestrator/test_join_delegation_step_seam.py",
    "tests/nodes/node_delegation_code_budget_compute/test_golden_chain_code_budget.py",
    "tests/nodes/node_delegation_failure_explanation_compute/test_failure_explanation.py",
    "tests/nodes/node_delegation_failure_explanation_compute/test_golden_chain_delegation_failure_explanation_compute.py",
    "tests/nodes/node_dod_closeout_sweep_compute/test_closer_rank_last_examined_dod_closeout_sweep_compute.py",
    "tests/nodes/node_floor_compare_effect/test_repository_names.py",
    "tests/nodes/node_floor_emission_compute/test_join_downstream_failed_after_success.py",
    "tests/nodes/node_floor_simple_compute/test_unticketed_prs_floor_row.py",
    "tests/nodes/node_join_refusal_effect/test_join_j6_journal_seam.py",
    "tests/nodes/node_lane_toolchain_provision_effect/test_crush_declared.py",
    "tests/nodes/node_linear_snapshot_effect/test_ancestors.py",
    "tests/nodes/node_linear_snapshot_effect/test_parent_and_project_id.py",
    "tests/nodes/node_pr_landing_watcher_facts_effect/test_watcher_facts.py",
    "tests/nodes/node_reading_writer_effect/test_append_command_stderr_kept.py",
    "tests/nodes/node_reading_writer_effect/test_join_read_budget_to_floor_omn23519.py",
    "tests/nodes/node_reading_writer_effect/test_read_budget_omn23119.py",
    "tests/nodes/node_seam_deadline_probe_effect/test_seam_deadline_recheck.py",
    "tests/nodes/node_work_state_projection_compute/test_work_state_ledger_single_pass.py",
    "tests/nodes/node_work_state_projection_compute/test_work_state_scope_parent_chain.py",
    "tests/nodes/node_work_state_projection_compute/test_work_state_ticket_scope_overlay.py",
    "tests/test_omn21607_failed_acceptance_real_postgres.py",
    "tests/unit/delegation/test_failed_acceptance_shows_failed_omn21607.py",
    "tests/unit/nodes/node_overlay_store/test_fleet_resume_overlay_store.py",
    "tests/unit/nodes/node_typed_decision_effect/test_typed_decision_event_omn23334.py",
    "tests/unit/nodes/node_validator_verdict_reducer/test_validator_verdict_ratchet.py",
    "tests/unit/nodes/node_validator_workflow_orchestrator/test_validator_workflow.py",
    "tests/unit/nodes/node_work_ledger_query_effect/test_boot_priority_name_seam.py",
    "tests/unit/nodes/node_work_ledger_query_effect/test_inmemory_rows_matching_exact_kinds.py",
    "tests/unit/nodes/node_work_ledger_query_effect/test_parity_read_bounded_on_todays_ledger.py",
    "tests/unit/nodes/node_work_ledger_query_effect/test_rows_read_narrowed_in_store.py",
    "tests/unit/nodes/test_golden_chain_node_ci_summary_registry_compute.py",
)


def test_files_unrecorded_before_omn_23538_are_in_the_record() -> None:
    record = json.loads(RECORD.read_text(encoding="utf-8"))
    missing = [path for path in RECORDED_BY_OMN_23538 if path not in record]
    assert not missing, (
        f"{len(missing)} test files missing from {RECORD.name}: {missing}"
    )
