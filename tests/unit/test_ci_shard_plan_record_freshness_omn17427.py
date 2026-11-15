# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-17427: the committed durations record leaves few test files unrecorded."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "ci" / "merge_test_durations.py"
RECORD = REPO_ROOT / "config" / "test_file_durations.json"
# The 5% guard in test_ci_shard_plan_omn19684 fires at about 160 files; a
# freshly regenerated record leaves only a handful, so this keeps real headroom.
MAX_UNRECORDED_FILES = 50


def test_committed_record_leaves_fewer_than_fifty_test_files_unrecorded() -> None:
    spec = importlib.util.spec_from_file_location("merge_test_durations_fresh", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    record = module.read_record(RECORD)
    files = module.discover_test_files(REPO_ROOT)
    _, unrecorded = module.file_weights(files, record)
    assert len(unrecorded) < MAX_UNRECORDED_FILES, (
        f"{len(unrecorded)} of {len(files)} test files are not in {RECORD.name}; "
        "regenerate it with merge_test_durations.py record"
    )


def test_the_post_merge_limit_is_the_limit_of_this_test() -> None:
    """OMN-23537: the freshness run after a merge and this test stop at the same count."""
    from omnimarket.nodes.node_test_duration_record_check_effect.adapters.policy_file import (
        load_policy,
    )

    assert load_policy().max_unrecorded_files == MAX_UNRECORDED_FILES


# OMN-23054: test files that dev's record did not hold, now recorded as entry files.
OMN_23054_RECORDED_FILES = (
    "tests/nodes/node_delegated_code_edit_orchestrator/test_unreceipted_model_turn.py",
    "tests/nodes/node_delegation_code_budget_compute/test_chains_input_ceiling.py",
    "tests/nodes/node_delegation_code_budget_compute/test_input_ceiling.py",
    "tests/nodes/node_delegation_code_budget_compute/test_seam_input_ceiling_overlay_routing.py",
    "tests/nodes/node_dev_deploy_reading_compute/test_deploy_agent_source.py",
    "tests/nodes/node_dev_deploy_reading_effect/test_dev_deploy_agent_source.py",
    "tests/nodes/node_factory_floor_effect/test_ledger_source_named.py",
    "tests/nodes/node_factory_floor_publish_effect/test_friction_paging_seam.py",
    "tests/nodes/node_factory_floor_publish_effect/test_page_readiness_seam_omn23522.py",
    "tests/nodes/node_factory_floor_publish_effect/test_step_environment.py",
    "tests/nodes/node_factory_floor_publish_effect/test_unresolved_step_seam.py",
    "tests/nodes/node_factory_floor_unresolved_effect/test_factory_floor_unresolved.py",
    "tests/nodes/node_floor_delegation_effect/test_seam_ledger_bounded_rows.py",
    "tests/nodes/node_host_installer_registry_units/test_landing_systemd_units.py",
    "tests/nodes/node_host_installer_registry_units/test_landing_template_args.py",
    "tests/nodes/node_host_installer_registry_units/test_memory_pilot_daily_unit.py",
    "tests/nodes/node_host_installer_registry_units/test_pilot_measurement_hourly_unit.py",
    "tests/nodes/node_host_installer_registry_units/test_start_check_real_args.py",
    "tests/nodes/node_memory_pilot_effect/test_lab_chat_post.py",
    "tests/nodes/node_memory_pilot_effect/test_pilot_run.py",
    "tests/nodes/node_memory_pilot_effect/test_pilot_run_seam.py",
    "tests/nodes/node_pilot_measurement_effect/test_pilot_measurement_tick.py",
    "tests/nodes/node_pr_duplicate_guard_compute/test_pr_duplicate_guard_replaces.py",
    "tests/nodes/node_pr_landing_snapshot_effect/test_controller_carry.py",
    "tests/nodes/node_pr_landing_snapshot_effect/test_join_controller_carry.py",
    "tests/nodes/node_pr_landing_tick_orchestrator/test_controller_compare.py",
    "tests/nodes/node_pr_landing_tick_orchestrator/test_controller_replay.py",
    "tests/nodes/node_pr_landing_tick_orchestrator/test_tick_compared_grants_seam.py",
    "tests/nodes/node_reading_compute/test_reading_unknown_source.py",
    "tests/nodes/node_work_ledger_file_mode_replay_effect/test_replay_from_file.py",
    "tests/nodes/node_work_state_projection_scheduled_effect/test_state_home.py",
    "tests/test_delegation_runtime_delivery_aggregate_coalescing.py",
    "tests/unit/delegation/test_probe_rung_credential.py",
    "tests/unit/handlers/scm/test_scm_duplicate_guard_replaces.py",
)


def test_omn23054_files_are_recorded_and_exist() -> None:
    spec = importlib.util.spec_from_file_location("merge_test_durations_23054", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    record = module.read_record(RECORD)
    missing = [f for f in OMN_23054_RECORDED_FILES if f not in record]
    assert not missing, f"test files with no recorded duration: {missing}"
    gone = [f for f in OMN_23054_RECORDED_FILES if not (REPO_ROOT / f).is_file()]
    assert not gone, f"recorded test files that do not exist: {gone}"
