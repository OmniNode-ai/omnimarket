# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23526: the durations record holds the test files dev left out (57 when ticketed).

The file name keeps the ``omn23527`` spelling the ticket's AC1 falsifier names.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

CONFIG = Path(__file__).resolve().parents[2] / "config"
RECORD = CONFIG / "test_file_durations.json"
PROVENANCE = CONFIG / "test_file_durations_provenance.json"

MEASURED_FILES: tuple[str, ...] = (
    "tests/chain/test_landing_snapshot_grant_seam.py",
    "tests/ci/test_repo_evidence_preflight_startup_failure_omn23087.py",
    "tests/integration/test_join_j7.py",
    "tests/nodes/node_c32_auth_boundary_probe_effect/test_c32_probe_node.py",
    "tests/nodes/node_c32_auth_boundary_probe_effect/test_probe_cases.py",
    "tests/nodes/node_context_additive_latency_compute/test_additive_latency.py",
    "tests/nodes/node_dod_closeout_sweep_compute/test_closer_rank_last_examined_dod_closeout_sweep_compute.py",
    "tests/nodes/node_factory_floor_effect/test_placement_tolerant_reader.py",
    "tests/nodes/node_floor_compare_effect/test_repository_names.py",
    "tests/nodes/node_floor_delegation_effect/test_cached_scenario_reads.py",
    "tests/nodes/node_floor_delegation_effect/test_delegation_routes.py",
    "tests/nodes/node_floor_emission_compute/test_join_downstream_failed_after_success.py",
    "tests/nodes/node_floor_fill_candidates_compute/test_join_closing_row_needs_to_fill.py",
    "tests/nodes/node_floor_fill_orchestrator/test_node_floor_fill_orchestrator_continuation.py",
    "tests/nodes/node_floor_fill_orchestrator/test_node_floor_fill_orchestrator_continuation_seams.py",
    "tests/nodes/node_floor_simple_compute/test_readiness_omn23081.py",
    "tests/nodes/node_floor_simple_compute/test_unticketed_prs_floor_row.py",
    "tests/nodes/node_github_repo_gateway_effect/test_job_log_read_fills_on_miss.py",
    "tests/nodes/node_host_installer_registry_units/test_alarm_dispatch_command.py",
    "tests/nodes/node_host_installer_registry_units/test_landing_snapshot_unit.py",
    "tests/nodes/node_host_reading_effect/test_host_reading_overlay_venv.py",
    "tests/nodes/node_join_refusal_effect/test_join_j7_seam.py",
    "tests/nodes/node_lab_fill_selection_compute/test_lab_fill_already_built.py",
    "tests/nodes/node_lab_idle_cost_compute/test_lab_idle_cost_contract.py",
    "tests/nodes/node_linear_snapshot_effect/test_ancestors.py",
    "tests/nodes/node_linear_snapshot_effect/test_parent_and_project_id.py",
    "tests/nodes/node_linear_ticket_write_effect/test_join_ticket_runner_seam.py",
    "tests/nodes/node_pr_landing_tick_orchestrator/test_act_out_of_scope_record.py",
    "tests/nodes/node_pr_landing_tick_orchestrator/test_tick_facts_topic_provisioned.py",
    "tests/nodes/node_pr_landing_tick_orchestrator/test_tick_topic_reader.py",
    "tests/nodes/node_reading_writer_effect/test_append_command_stderr_kept.py",
    "tests/nodes/node_reading_writer_effect/test_reading_units_overlay_venv.py",
    "tests/nodes/node_remote_lane_close_compute/test_join_closing_row_needs.py",
    "tests/nodes/node_remote_lane_close_compute/test_join_needs_fill_reader.py",
    "tests/nodes/node_remote_lane_close_compute/test_lane_needs_omn23417.py",
    "tests/nodes/node_seam_deadline_probe_effect/test_seam_deadline_recheck.py",
    "tests/nodes/node_stability_lane_refresh_effect/test_stability_lane_chain_events.py",
    "tests/nodes/node_stability_lane_refresh_effect/test_stability_lane_real_profile.py",
    "tests/nodes/node_stability_lane_refresh_effect/test_stability_lane_refresh.py",
    "tests/nodes/node_tenant_delegations_read_effect/test_fail_closed.py",
    "tests/nodes/node_tenant_delegations_read_effect/test_golden_chain_tenant_delegations_read.py",
    "tests/nodes/node_tenant_delegations_read_effect/test_http_runtime_profiles.py",
    "tests/nodes/node_tenant_delegations_read_effect/test_t18b_replay.py",
    "tests/nodes/node_tenant_delegations_read_effect/test_tenant_delegations_http.py",
    "tests/nodes/node_tenant_savings_read_effect/test_fail_closed.py",
    "tests/nodes/node_tenant_savings_read_effect/test_golden_chain_tenant_savings_read.py",
    "tests/nodes/node_tenant_savings_read_effect/test_http_runtime_profiles.py",
    "tests/nodes/node_tenant_savings_read_effect/test_mount_contract.py",
    "tests/nodes/node_tenant_savings_read_effect/test_t18b_replay.py",
    "tests/nodes/node_tenant_savings_read_effect/test_tenant_savings_http.py",
    "tests/nodes/node_validator_estate_reading_effect/test_validator_estate.py",
    "tests/nodes/node_validator_estate_reading_effect/test_validator_estate_schedule.py",
    "tests/nodes/node_venv_atomic_install_effect/test_golden_chain_venv_atomic_install.py",
    "tests/nodes/node_venv_atomic_install_effect/test_measured_event_budget.py",
    "tests/nodes/node_work_state_projection_compute/test_work_state_scope_parent_chain.py",
    "tests/nodes/node_work_state_projection_compute/test_work_state_ticket_scope_overlay.py",
    "tests/nodes/node_work_state_projection_effect/test_publish_work_state_projection.py",
    "tests/nodes/node_work_state_projection_scheduled_effect/test_bus_roundtrip_work_state_projected.py",
    "tests/nodes/node_work_state_projection_scheduled_effect/test_contract_topics.py",
    "tests/nodes/node_work_state_projection_scheduled_effect/test_golden_chain_work_state_projection_scheduled.py",
    "tests/nodes/node_work_state_projection_scheduled_effect/test_seam_projection_file_floor_and_fill.py",
    "tests/test_golden_chain_context_additive_latency_compute.py",
    "tests/unit/lab_work/test_lab_host_table_ilo_address.py",
    "tests/unit/nodes/node_delegate_skill_orchestrator/test_customer_retry_wait_omn23176.py",
    "tests/unit/nodes/node_lab_check_run_effect/test_j7_caller_run.py",
    "tests/unit/nodes/node_typed_decision_effect/test_typed_decision_event_omn23334.py",
    "tests/unit/test_ci_duration_record_supplement_omn23527.py",
)


def test_the_measured_file_list_holds_the_57_files_the_ticket_counted_and_the_newer_ones() -> (
    None
):
    assert len(set(MEASURED_FILES)) == len(MEASURED_FILES)
    assert len(MEASURED_FILES) >= 57


@pytest.mark.parametrize("test_file", MEASURED_FILES)
def test_omn23526_supplement_records_each_measured_file(test_file: str) -> None:
    record = json.loads(RECORD.read_text(encoding="utf-8"))
    assert test_file in record, f"measured test file missing from record: {test_file}"
    seconds = record[test_file]
    assert isinstance(seconds, (int, float))
    assert not isinstance(seconds, bool)
    assert math.isfinite(seconds)
    assert seconds >= 0


def test_omn23526_supplement_has_provenance_entry() -> None:
    provenance = json.loads(PROVENANCE.read_text(encoding="utf-8"))
    entry = provenance["supplement_omn23526"]
    assert entry["files_added"] == 53
    assert entry["ticket"] == "OMN-23526"
