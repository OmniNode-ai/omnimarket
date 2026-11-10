# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The duration supplement retains measurements still absent from dev."""

from __future__ import annotations

import importlib.util
import json
import math
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "ci" / "merge_test_durations.py"
RECORD = REPO_ROOT / "config" / "test_file_durations.json"
OMN23469_FILES = (
    "tests/chain/test_landing_grant_seam.py",
    "tests/integration/test_join_j7.py",
    "tests/nodes/node_andon_threshold_compute/test_andon_measure_and_burst_scope.py",
    "tests/nodes/node_c32_auth_boundary_probe_effect/test_c32_probe_node.py",
    "tests/nodes/node_c32_auth_boundary_probe_effect/test_probe_cases.py",
    "tests/nodes/node_create_ticket/test_core_ticket_guard.py",
    "tests/nodes/node_factory_floor_effect/test_placement_tolerant_reader.py",
    "tests/nodes/node_floor_compare_effect/test_categories.py",
    "tests/nodes/node_floor_fill_orchestrator/test_node_floor_fill_orchestrator_continuation.py",
    "tests/nodes/node_floor_fill_orchestrator/test_node_floor_fill_orchestrator_continuation_seams.py",
    "tests/nodes/node_floor_task_ticket/test_floor_task_ticket_mint.py",
    "tests/nodes/node_floor_task_ticket/test_floor_task_ticket_old_fill.py",
    "tests/nodes/node_floor_task_ticket/test_floor_task_ticket_parity.py",
    "tests/nodes/node_floor_task_ticket/test_node_floor_task_ticket_effect.py",
    "tests/nodes/node_floor_task_ticket/test_node_floor_task_ticket_graphql_store.py",
    "tests/nodes/node_floor_task_ticket/test_node_floor_task_ticket_payload_compute.py",
    "tests/nodes/node_host_reading_effect/test_host_reading_overlay_venv.py",
    "tests/nodes/node_join_refusal_effect/test_join_j7_seam.py",
    "tests/nodes/node_lab_fill_effect/test_merged_history_lab_fill.py",
    "tests/nodes/node_lab_fill_plan_compute/test_lab_fill_closer_route.py",
    "tests/nodes/node_linear_ticket_write_effect/test_join_ticket_runner_seam.py",
    "tests/nodes/node_pr_landing_snapshot_effect/test_join_landing_snapshot_tick.py",
    "tests/nodes/node_pr_landing_snapshot_effect/test_landing_snapshot.py",
    "tests/nodes/node_pr_landing_snapshot_effect/test_landing_snapshot_replay.py",
    "tests/nodes/node_pr_landing_snapshot_effect/test_snapshot_real_backend.py",
    "tests/nodes/node_reading_writer_effect/test_reading_units_overlay_venv.py",
    "tests/nodes/node_remote_lane_close_compute/test_join_closing_row_needs.py",
    "tests/nodes/node_remote_lane_close_compute/test_join_needs_fill_reader.py",
    "tests/nodes/node_remote_lane_close_compute/test_lane_needs_omn23417.py",
    "tests/nodes/node_seam_deadline_probe_effect/test_seam_late_reply.py",
    "tests/nodes/node_seam_deadline_probe_effect/test_seam_row_consumers.py",
    "tests/nodes/node_work_state_projection_scheduled_effect/test_bus_roundtrip_work_state_projected.py",
    "tests/nodes/node_work_state_projection_scheduled_effect/test_contract_topics.py",
    "tests/nodes/node_work_state_projection_scheduled_effect/test_golden_chain_work_state_projection_scheduled.py",
    "tests/nodes/node_work_state_projection_scheduled_effect/test_seam_projection_file_floor_and_fill.py",
    "tests/test_node_dispatch_reading_compute_events.py",
    "tests/unit/nodes/node_lab_check_run_effect/test_j7_caller_run.py",
    "tests/unit/nodes/node_pr_landing_decision_compute/test_command_topic_provisioned.py",
)


@pytest.mark.parametrize(
    "test_file",
    [
        "tests/nodes/node_exception_queue_effect/test_andon_ledger_holds.py",
        "tests/test_golden_chain_projection_write_effect.py",
        "tests/test_omn18883_projection_write_effect_real_postgres.py",
        "tests/test_retired_repository_reconcile.py",
        "tests/unit/nodes/node_work_ledger_append_effect/test_append_if_absent.py",
    ],
)
def test_supplement_retains_the_remaining_measured_files(test_file: str) -> None:
    record = json.loads(RECORD.read_text(encoding="utf-8"))
    assert test_file in record, f"measured test file missing from record: {test_file}"
    seconds = record[test_file]
    assert isinstance(seconds, (int, float))
    assert not isinstance(seconds, bool)
    assert math.isfinite(seconds)
    assert seconds > 0


@pytest.mark.parametrize(
    "test_file",
    [
        "tests/integration/test_join_j16.py",
        "tests/nodes/node_factory_floor_publish_effect/test_compare_page.py",
        "tests/nodes/node_factory_floor_publish_effect/test_join_j2_step.py",
        "tests/nodes/node_factory_floor_publish_effect/test_join_refusal_step.py",
        "tests/nodes/node_factory_floor_publish_effect/test_simple_page_piece_measure.py",
        "tests/nodes/node_floor_compare_effect/test_piece_measure.py",
        "tests/nodes/node_floor_emission_compute/test_refusal_is_not_an_upstream_failure.py",
        "tests/unit/test_omn22867_shared_red_causes.py",
        "tests/validation/test_j8_join_is_not_auto_wirable_omn23076.py",
    ],
)
def test_omn23054_supplement_records_each_measured_file(test_file: str) -> None:
    record = json.loads(RECORD.read_text(encoding="utf-8"))
    assert test_file in record, f"measured test file missing from record: {test_file}"
    seconds = record[test_file]
    assert isinstance(seconds, (int, float))
    assert not isinstance(seconds, bool)
    assert math.isfinite(seconds)
    assert seconds >= 0


def test_omn23054_supplement_has_provenance_entry() -> None:
    provenance = json.loads(
        (RECORD.parent / "test_file_durations_provenance.json").read_text(
            encoding="utf-8"
        )
    )
    assert provenance["supplement_omn23054"]["files_added"] == 9


@pytest.mark.parametrize("test_file", OMN23469_FILES)
def test_omn23469_regeneration_records_each_unrecorded_file(test_file: str) -> None:
    record = json.loads(RECORD.read_text(encoding="utf-8"))
    assert test_file in record, f"unrecorded test file missing from record: {test_file}"
    seconds = record[test_file]
    assert isinstance(seconds, (int, float))
    assert not isinstance(seconds, bool)
    assert math.isfinite(seconds)
    assert math.isclose(seconds, 0.17)


def test_omn23469_regeneration_has_provenance_entry() -> None:
    provenance = json.loads(
        (RECORD.parent / "test_file_durations_provenance.json").read_text(
            encoding="utf-8"
        )
    )
    assert provenance["supplement_omn23469"]["files_added"] == 38


def test_omn23469_pinned_files_are_recorded_and_the_freshness_limit_is_unchanged() -> (
    None
):
    spec = importlib.util.spec_from_file_location(
        "merge_test_durations_omn23469", SCRIPT
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    record = json.loads(RECORD.read_text(encoding="utf-8"))
    files = module.discover_test_files(REPO_ROOT)
    _, unrecorded = module.file_weights(files, record)
    missing = sorted(set(OMN23469_FILES) & set(unrecorded))
    assert not missing, f"pinned test files reported unrecorded: {missing}"

    freshness_spec = importlib.util.spec_from_file_location(
        "test_ci_shard_plan_record_freshness_omn17427_omn23469",
        REPO_ROOT
        / "tests"
        / "unit"
        / "test_ci_shard_plan_record_freshness_omn17427.py",
    )
    assert freshness_spec is not None
    assert freshness_spec.loader is not None
    freshness_module = importlib.util.module_from_spec(freshness_spec)
    sys.modules[freshness_spec.name] = freshness_module
    freshness_spec.loader.exec_module(freshness_module)
    assert freshness_module.MAX_UNRECORDED_FILES == 50
