# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-23658: the duration record lists the test files that were unrecorded."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

RECORD = Path(__file__).resolve().parents[2] / "config" / "test_file_durations.json"

RECORDED_BY_OMN_23658 = (
    "tests/handlers/test_scheduled_overlay_refusal_once.py",
    "tests/nodes/node_broker_acl_overlay_render_compute/test_overlay_seams.py",
    "tests/nodes/node_broker_acl_overlay_render_compute/test_render_error_chain.py",
    "tests/nodes/node_broker_acl_overlay_render_compute/test_render_golden_chain.py",
    "tests/nodes/node_canonical_clone_refresh_effect/test_declared_clone_tick.py",
    "tests/nodes/node_delegation_scenario_r3_effect/test_scenario_settings_overlay_seam.py",
    "tests/nodes/node_delegation_scenario_r3_effect/test_settings_fill.py",
    "tests/nodes/node_delegation_scenario_r8_effect/test_golden_chain_scenario_r8_effect.py",
    "tests/nodes/node_delegation_scenario_r8_effect/test_r8_inputs.py",
    "tests/nodes/node_dev_deploy_reading_compute/test_lineage_coverage.py",
    "tests/nodes/node_dev_deploy_reading_effect/test_dev_deploy_lineage_facts.py",
    "tests/nodes/node_factory_floor_compute/test_linear_snapshot_children_seam.py",
    "tests/nodes/node_factory_floor_compute/test_stopped_lane_child_tickets.py",
    "tests/nodes/node_floor_fill_reducer/test_floor_fill_reducer_ac7_selection.py",
    "tests/nodes/node_floor_fill_reducer/test_floor_fill_reducer_contract.py",
    "tests/nodes/node_host_installer_registry_units/test_declared_clone_refresh_unit.py",
    "tests/nodes/node_host_installer_registry_units/test_landing_mains_probe.py",
    "tests/nodes/node_host_installer_registry_units/test_lane_closeout_sweep_launchd_unit.py",
    "tests/nodes/node_host_installer_registry_units/test_lane_closeout_sweep_unit.py",
    "tests/nodes/node_host_installer_registry_units/test_start_check_environment.py",
    "tests/nodes/node_host_installer_registry_units/test_start_probe.py",
    "tests/nodes/node_join_j16_effect/test_join_j16_floor_seam.py",
    "tests/nodes/node_join_refusal_effect/test_join_j6_terminal_codes_seam.py",
    "tests/nodes/node_lane_closeout_sweep_effect/test_golden_chain_lane_closeout_sweep.py",
    "tests/nodes/node_lane_closeout_sweep_effect/test_process_cwds_without_proc.py",
    "tests/nodes/node_pilot_measurement_effect/test_pilot_measurement_chain_events.py",
    "tests/nodes/node_pilot_measurement_effect/test_pilot_measurement_file_source.py",
    "tests/nodes/node_pilot_measurement_effect/test_pilot_measurement_overlay.py",
    "tests/nodes/node_pilot_measurement_effect/test_pilot_measurement_reading.py",
    "tests/nodes/node_pilot_measurement_effect/test_pilot_measurement_reading_seam.py",
    "tests/nodes/node_pilot_measurement_effect/test_pilot_measurement_schedule.py",
    "tests/nodes/node_pr_landing_snapshot_effect/test_join_liveness_fact.py",
    "tests/nodes/node_pr_landing_snapshot_effect/test_snapshot_absent_overlay.py",
    "tests/nodes/node_pr_landing_tick_orchestrator/test_tick_bus_publisher.py",
    "tests/nodes/node_prrp_snapshot_publish_effect/test_prrp_snapshot_hosts_from_lab_table.py",
    "tests/nodes/node_pytest_full_suite_host_guard/test_prepush_logins_overlay_model.py",
    "tests/nodes/node_runner_group_exposure_effect/test_token_mint.py",
    "tests/nodes/node_unit_tree_install_effect/test_error_chain_unit_tree_install.py",
    "tests/nodes/node_unit_tree_install_effect/test_golden_chain_unit_tree_install.py",
    "tests/nodes/node_unit_tree_install_effect/test_runtime_entry_unit_tree_install.py",
    "tests/nodes/node_unit_tree_install_effect/test_unit_tree_install.py",
    "tests/nodes/test_golden_chain_lane_dispatch_effect.py",
    "tests/nodes/test_lane_dispatch_batch.py",
    "tests/nodes/test_lane_dispatch_contract.py",
    "tests/nodes/test_scenario_unit_environment_workspace_root.py",
    "tests/test_delegation_eval_runtime_binding.py",
    "tests/test_local_environment_retired_service_omn22030.py",
    "tests/test_local_environment_retired_service_real_postgres_omn22030.py",
    "tests/test_omn19998_local_identity_reader_optout.py",
    "tests/unit/declared_clone_tick/test_declared_clone_tick_seam.py",
    "tests/unit/delegation/test_no_paid_run_never_reaches_paid_rung_omn18308.py",
    "tests/unit/nodes/node_dod_verify/test_omn_23198_venv_cleanup.py",
    "tests/unit/nodes/node_onex_setup_effect/test_onex_setup_model_step.py",
    "tests/unit/nodes/node_pr_landing_decision_compute/test_liveness_hold.py",
    "tests/unit/nodes/node_pr_landing_liveness_read_effect/test_liveness_read.py",
    "tests/unit/nodes/node_reading_writer_effect/test_final_pass_omn23588.py",
    "tests/unit/nodes/node_reading_writer_effect/test_join_final_pass_to_floor_omn23588.py",
    "tests/unit/nodes/node_work_ledger_append_effect/test_join_j5_terminal.py",
    "tests/unit/nodes/node_work_ledger_query_effect/test_rows_read_narrowed_without_start.py",
    "tests/unit/workflows/test_merge_group_asserted_contexts_covered_omn19998.py",
)


def test_files_unrecorded_before_omn_23658_are_in_the_record() -> None:
    record = json.loads(RECORD.read_text(encoding="utf-8"))
    missing = [path for path in RECORDED_BY_OMN_23658 if path not in record]
    assert not missing, (
        f"{len(missing)} test files missing from {RECORD.name}: {missing}"
    )
