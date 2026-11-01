# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A fixture test may not hand-write the event payload its producer emits.

THE DEFECT CLASS
----------------
A projection or consumer test that builds its input as a dict literal stays green
while the producer changes. One such fixture carried 8 keys where production emitted
15, and the three it omitted were the three the real record did not declare, so the
real ladder refused the write in CI while the fixture passed. A consumer whose request
model wrapped the payload under a key the emitter never sent was the same defect: a
fixture built from the emitter's own model could not have carried that wrapper.

THE RULE
--------
Inside the in-scope projection and consumer test directories, a dict literal with
three or more string keys that is assigned to, returned as, or passed as a payload,
event or envelope is refused. Derive it from the producer (its model, its builder, a
recorded envelope) instead.

The scan walks the syntax tree. A line regex finds a fraction of these sites because
nearly every call spans lines, so none is used here.

THE FENCE, AND WHY IT IS NOT A BASELINE
---------------------------------------
Files that hand-write a payload today are held by ``_FENCED``. A baseline grows
whenever a new defect appears; this table is keyed on the exact (path, site, count)
triple and a row that no longer matches is a hard FAILURE (``stale``), so it cannot
rot and cannot be topped up. A new site in a fenced file exceeds its row's count and
fails as an unfenced violation. Rows only ever leave the table.

THE SCAN FLOOR
--------------
A scan that finds far fewer files than the tree has is a broken scan, not a clean
tree. ``DEFAULT_MIN_EXPECTED_FILES`` fails the run below that count.

REPORT-ONLY TIER
----------------
A string-keyed dict literal of three or more keys that is not named like a payload is
ambiguous: roughly half are projection rows, not payloads. They are counted and
printed, never blocking, until the row/payload distinction is encoded.
"""

from __future__ import annotations

import ast
import re
from collections import Counter
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from ..models.model_handwritten_event_payload_check import (
    ModelHandwrittenEventPayloadCheckRequest,
    ModelHandwrittenEventPayloadCheckResult,
)

TAG = "handwritten-event-payload"

#: In-scope fixture directories and files, relative to the repository root. Only 13 of
#: the 41 projection packages carry in-package tests, so a scope of one directory
#: family would read as coverage while covering a fraction of the fixtures.
SCOPE_GLOBS: tuple[str, ...] = (
    "tests/chains/**/*.py",
    "tests/golden_chains/**/*.py",
    "tests/projection/**/*.py",
    "tests/nodes/node_projection_*/**/*.py",
    "tests/unit/nodes/node_projection_*/**/*.py",
    "src/omnimarket/nodes/node_projection_*/tests/**/*.py",
    "tests/test_*consumer*.py",
    "tests/test_*projection*.py",
    "tests/test_omn18889*.py",
)

#: A scan that finds far fewer files than the tree has is a broken scan.
DEFAULT_MIN_EXPECTED_FILES = 300

MIN_KEYS = 3

_PAYLOAD_KWARGS = frozenset(
    {"payload", "event", "envelope", "event_payload", "raw_event", "wire_payload"}
)
_PAYLOADISH = re.compile(r"(payload|envelope|event|message)", re.IGNORECASE)
_NOT_PAYLOAD = re.compile(
    r"(row|expected|want|schema|config|header|meta|filter|param|kwargs|snapshot|_by_)",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------------------
# THE FENCE: (path, site, count). Generated once from the tree and only ever shrunk.
# ---------------------------------------------------------------------------------------
_FENCED: tuple[tuple[str, str, int], ...] = (
    (
        "src/omnimarket/nodes/node_projection_delegation/tests/test_delegation_keeps_counterfactual.py",
        "_canonical_payload:ret:_canonical_payload",
        1,
    ),
    (
        "src/omnimarket/nodes/node_projection_delegation/tests/test_delegation_keeps_counterfactual.py",
        "_delegate_skill_payload:ret:_delegate_skill_payload",
        1,
    ),
    (
        "src/omnimarket/nodes/node_projection_event_chain/tests/test_golden_chain_event_chain_node.py",
        "_payload:ret:_payload",
        1,
    ),
    (
        "src/omnimarket/nodes/node_projection_live_events/tests/test_golden_chain_projection_live_events.py",
        "TestProjectionLiveEventsGoldenChain.test_platform_log_event_materializes_live_event_row:asg:raw_event",
        1,
    ),
    (
        "src/omnimarket/nodes/node_projection_live_events/tests/test_projection_live_events.py",
        "TestHandlerProjectionLiveEventsHandle.test_handle_deduplicates_redelivery_by_typed_envelope_id:asg:payload",
        1,
    ),
    (
        "src/omnimarket/nodes/node_projection_live_events/tests/test_projection_live_events.py",
        "_make_delegation_event:ret:_make_delegation_event",
        1,
    ),
    (
        "src/omnimarket/nodes/node_projection_live_events/tests/test_projection_live_events.py",
        "_make_log_event:ret:_make_log_event",
        1,
    ),
    (
        "src/omnimarket/nodes/node_projection_llm_cost/tests/test_row_and_backfill_llm_call_metrics.py",
        "TestRealEventPayload.test_canonical_event_maps_correctly:asg:event",
        1,
    ),
    (
        "src/omnimarket/nodes/node_projection_receipt_gate/tests/test_handler_receipt_gate.py",
        "TestEvidenceValidatedProjection.test_evidence_validated_writes_one_occ_row:asg:event",
        1,
    ),
    (
        "src/omnimarket/nodes/node_projection_receipt_gate/tests/test_handler_receipt_gate.py",
        "TestVerificationReceiptProjection.test_one_row_is_written_per_check_dimension:asg:event",
        1,
    ),
    (
        "src/omnimarket/nodes/node_projection_routing_decision/tests/test_golden_chain_routing_decision.py",
        "<module>:asg:_ROUTING_PAYLOAD",
        1,
    ),
    (
        "src/omnimarket/nodes/node_projection_savings/tests/test_savings_one_row_per_run.py",
        "_canonical_payload:ret:_canonical_payload",
        1,
    ),
    (
        "src/omnimarket/nodes/node_projection_savings/tests/test_savings_one_row_per_run.py",
        "_delegate_skill_payload:ret:_delegate_skill_payload",
        1,
    ),
    (
        "tests/chains/delegation/obligations.py",
        "reduce_walker_workflow:ret:reduce_walker_workflow",
        1,
    ),
    ("tests/chains/generated/test_event_chain_gate.py", "_step:ret:_step", 1),
    ("tests/chains/test_event_chain_gate.py", "<module>:kw:wire_payload", 1),
    (
        "tests/chains/test_event_chain_gate_projection_write.py",
        "<module>:kw:wire_payload",
        2,
    ),
    (
        "tests/golden_chains/superseded_run_cancel/test_golden_chain_superseded_run_cancel.py",
        "_run:ret:_run",
        1,
    ),
    (
        "tests/nodes/node_projection_capsule_store/test_effectiveness_populated_from_roi.py",
        "TestEffectivenessFromRoi.test_from_roi_result_view_maps_fields:asg:payload",
        1,
    ),
    (
        "tests/nodes/node_projection_capsule_store/test_golden_chain_capsule_store.py",
        "<module>:asg:_SCORE_PAYLOAD",
        1,
    ),
    (
        "tests/nodes/node_projection_delegation_inference_response/test_golden_chain_projection_delegation_inference_response.py",
        "test_handle_upserts_singleton_row:asg:payload",
        1,
    ),
    (
        "tests/nodes/node_projection_metering_summary/test_omn17427_store_refresh.py",
        "_run:ret:_run",
        1,
    ),
    (
        "tests/nodes/node_projection_receipt_gate/test_reducer_receipt_gate.py",
        "test_reducer_prepends_new_rows:asg:first_event",
        1,
    ),
    (
        "tests/nodes/node_projection_receipt_gate/test_reducer_receipt_gate.py",
        "test_reducer_prepends_new_rows:asg:second_event",
        1,
    ),
    (
        "tests/nodes/node_projection_receipt_gate_pin_friction/test_golden_chain_pin_friction_projection.py",
        "_head:ret:_head",
        1,
    ),
    (
        "tests/nodes/node_projection_tenant_registry/test_golden_chain_projection_tenant_registry.py",
        "_tenant_created_envelope:ret:_tenant_created_envelope",
        1,
    ),
    (
        "tests/projection/test_dashboard_bundle_failure_omn20705.py",
        "dashboard_env:ret:dashboard_env",
        1,
    ),
    (
        "tests/projection/test_omn19919_failure_cause_served.py",
        "terminal:asg:payload",
        1,
    ),
    ("tests/projection/test_omn21015_run_savings_served.py", "event:asg:payload", 1),
    (
        "tests/projection/test_omn21015_usage_tier_share.py",
        "test_u9_the_writer_locks_the_tenants_day_recounts_the_tier_and_republishes_the_day:asg:payload",
        1,
    ),
    (
        "tests/test_alert_channel_liveness_projection_real_postgres.py",
        "_result:ret:_result",
        1,
    ),
    (
        "tests/test_dispatch_path_projection_llm_cost.py",
        "<module>:asg:CANONICAL_EVENT",
        1,
    ),
    (
        "tests/test_event_chain_projection_omn13620.py",
        "test_replay_same_envelope_is_idempotent:asg:event",
        1,
    ),
    (
        "tests/test_generation_corpus_projection_omn13350.py",
        "_project_generation:asg:payload",
        1,
    ),
    (
        "tests/test_generation_corpus_projection_omn13350.py",
        "test_sync_path_full_payload_projects_without_undefined_column:asg:payload",
        1,
    ),
    (
        "tests/test_generation_semantic_pass_projection_omn13166.py",
        "_project_generation:asg:payload",
        1,
    ),
    (
        "tests/test_golden_chain_node_projection_board_probe_results.py",
        "_payload:ret:_payload",
        1,
    ),
    (
        "tests/test_golden_chain_projection_ci_attempt_outcome.py",
        "_event_one_check_per_cause:ret:_event_one_check_per_cause",
        1,
    ),
    (
        "tests/test_golden_chain_projection_consumer_flow.py",
        "test_golden_chain_end_to_end_separates_stalled_from_flowing._delta:ret:_delta",
        1,
    ),
    (
        "tests/test_golden_chain_projection_context_roi.py",
        "TestContextRoiProjection.test_handle_projects_failed_terminal_payload:asg:payload",
        1,
    ),
    (
        "tests/test_golden_chain_projection_context_roi.py",
        "TestContextRoiProjection.test_handle_strips_transport_metadata:asg:payload",
        1,
    ),
    (
        "tests/test_golden_chain_projection_delegation.py",
        "TestComplianceCounters.test_dict_payload_with_counters_via_handle_protocol:asg:payload",
        1,
    ),
    (
        "tests/test_golden_chain_projection_delegation.py",
        "TestCostFields.test_cost_fields_via_handle_protocol:asg:payload",
        1,
    ),
    (
        "tests/test_golden_chain_projection_delegation.py",
        "TestDelegationProjection.test_canonical_terminal_carries_authoritative_cost_tier:asg:payload",
        1,
    ),
    (
        "tests/test_golden_chain_projection_delegation.py",
        "TestDelegationProjection.test_canonical_terminal_without_tier_falls_back_to_metered_winner:asg:payload",
        1,
    ),
    (
        "tests/test_golden_chain_projection_delegation.py",
        "TestDelegationProjection.test_sparse_task_delegated_event_does_not_clear_terminal_evidence:asg:sparse_compat_payload",
        1,
    ),
    (
        "tests/test_golden_chain_projection_delegation.py",
        "TestDelegationProjection.test_sparse_task_delegated_event_does_not_clear_terminal_evidence:asg:terminal_payload",
        1,
    ),
    (
        "tests/test_golden_chain_projection_delegation.py",
        "TestDelegationProjection.test_sync_handler_projects_canonical_delegation_terminal_event:asg:payload",
        1,
    ),
    (
        "tests/test_golden_chain_projection_delegation.py",
        "TestDelegationProjection.test_sync_handler_projects_delegate_skill_terminal_event:asg:payload",
        1,
    ),
    (
        "tests/test_golden_chain_projection_delegation.py",
        "TestDelegationProjection.test_task_delegated_labels_project_quality_bar_evidence:asg:payload",
        1,
    ),
    (
        "tests/test_golden_chain_projection_delegation.py",
        "TestDelegationProjection.test_terminal_row_carries_created_at:asg:payload",
        1,
    ),
    (
        "tests/test_golden_chain_projection_delegation.py",
        "TestGenerationCompletedProjection._generation_payload:asg:payload",
        1,
    ),
    (
        "tests/test_golden_chain_projection_delegation.py",
        "TestNoBackfillMaterialization._fresh_terminal_payload:ret:_fresh_terminal_payload",
        1,
    ),
    (
        "tests/test_golden_chain_projection_delegation.py",
        "TestPricingManifestVersion.test_old_event_without_field_defaults_to_zero:asg:payload",
        1,
    ),
    (
        "tests/test_golden_chain_projection_delegation.py",
        "TestPricingManifestVersion.test_pricing_version_via_handle_protocol:asg:payload",
        1,
    ),
    (
        "tests/test_golden_chain_projection_delegation.py",
        "TestPromptResponseText.test_prompt_response_text_via_handle_protocol:asg:payload",
        1,
    ),
    (
        "tests/test_golden_chain_projection_delegation.py",
        "TestResponseTextTimeoutOnPass._canonical_pass_payload:ret:_canonical_pass_payload",
        1,
    ),
    (
        "tests/test_golden_chain_projection_delegation.py",
        "TestResponseTextTimeoutOnPass._timeout_terminal_payload:ret:_timeout_terminal_payload",
        1,
    ),
    (
        "tests/test_golden_chain_projection_delegation.py",
        "TestResponseTextTimeoutOnPass.test_pass_terminal_response_text_never_carries_error_message:asg:payload",
        1,
    ),
    (
        "tests/test_golden_chain_projection_delegation.py",
        "TestZeroTokenZeroCostTerminal._make_zero_token_terminal_payload:ret:_make_zero_token_terminal_payload",
        1,
    ),
    (
        "tests/test_golden_chain_projection_delegation.py",
        "TestZeroTokenZeroCostTerminal.test_real_token_terminal_event_accepted:asg:payload",
        1,
    ),
    (
        "tests/test_golden_chain_projection_delegation.py",
        "TestZeroTokenZeroCostTerminal.test_zero_tokens_but_nonzero_cost_accepted:asg:payload",
        1,
    ),
    (
        "tests/test_golden_chain_projection_dep_health.py",
        "TestProjectionDepHealthHandleShim.test_handle_dict_interface_materializes_rows:asg:payload",
        1,
    ),
    (
        "tests/test_golden_chain_projection_dep_health.py",
        "TestProjectionDepHealthHandleShim.test_handle_strips_injected_event_type_and_materializes:asg:payload",
        1,
    ),
    ("tests/test_golden_chain_projection_dod_verdict.py", "_event:asg:payload", 1),
    (
        "tests/test_golden_chain_projection_intent_classification.py",
        "TestAgentSourceSeam.test_camel_case_wire_keys_persist_through_handle:asg:camel_event",
        1,
    ),
    (
        "tests/test_golden_chain_projection_intent_classification.py",
        "TestAgentSourceSeam:asg:WIRE_EVENT",
        1,
    ),
    (
        "tests/test_golden_chain_projection_mcp_tools.py",
        "TestProjectionMcpToolsHandleInterface.test_handle_dict_mcp_eligible:asg:payload",
        1,
    ),
    (
        "tests/test_golden_chain_projection_mcp_tools.py",
        "TestProjectionMcpToolsHandleInterface.test_handle_dict_non_mcp:asg:payload",
        1,
    ),
    (
        "tests/test_golden_chain_projection_routing_decision.py",
        "<module>:asg:_ROUTING_PAYLOAD",
        1,
    ),
    (
        "tests/test_golden_chain_projection_runner_fleet.py",
        "_live_pool_observation:ret:_live_pool_observation",
        1,
    ),
    ("tests/test_golden_chain_projection_runner_fleet.py", "_runner:ret:_runner", 1),
    (
        "tests/test_golden_chain_projection_runtime_error_fingerprints.py",
        "_bridge_event:asg:payload",
        1,
    ),
    (
        "tests/test_golden_chain_projection_sandbox_decisions.py",
        "<module>:asg:COMPLETED_EVENT_DATA",
        1,
    ),
    (
        "tests/test_golden_chain_projection_sandbox_decisions.py",
        "<module>:asg:FAILED_EVENT_DATA",
        1,
    ),
    (
        "tests/test_golden_chain_projection_savings.py",
        "TestSavingsProjection.test_sync_handler_projects_delegate_skill_terminal_savings:asg:payload",
        1,
    ),
    (
        "tests/test_golden_chain_projection_swarm.py",
        "TestProjectionSwarmAppliedEvent.test_handle_dict_interface:asg:payload",
        1,
    ),
    (
        "tests/test_omn15905_delegation_projection_writer_seam.py",
        "_real_delegate_skill_terminal_payload:ret:_real_delegate_skill_terminal_payload",
        1,
    ),
    (
        "tests/test_omn15905_delegation_projection_writer_seam.py",
        "_real_delegation_completed_payload:ret:_real_delegation_completed_payload",
        1,
    ),
    (
        "tests/test_omn15905_delegation_projection_writer_seam.py",
        "_real_quality_gate_result_payload:ret:_real_quality_gate_result_payload",
        1,
    ),
    (
        "tests/test_omn15909_real_postgres_projection_write_path_gate.py",
        "_real_delegation_completed_payload:ret:_real_delegation_completed_payload",
        1,
    ),
    (
        "tests/test_omn15909_real_postgres_projection_write_path_gate.py",
        "_real_quality_gate_result_payload:ret:_real_quality_gate_result_payload",
        1,
    ),
    (
        "tests/test_omn16249_projection_injected_key_seam.py",
        "_gateway_shaped_batch:ret:_gateway_shaped_batch",
        1,
    ),
    (
        "tests/test_omn16316_tenant_credentials_projection.py",
        "TestBoundHost.test_tenant_credentials_bound_host_sqlite_store_writes_and_keeps_it:asg:event",
        1,
    ),
    (
        "tests/test_omn16316_tenant_credentials_projection.py",
        "TestExpiresAt.test_sqlite_store_writes_expires_at:asg:event",
        1,
    ),
    (
        "tests/test_omn16316_tenant_credentials_projection.py",
        "TestWorkspaceId.test_tenant_credentials_workspace_id_sqlite_store_writes_and_keeps_it:asg:event",
        1,
    ),
    (
        "tests/test_omn16690_projection_contract_access.py",
        "_gateway_wire_payload:ret:_gateway_wire_payload",
        1,
    ),
    ("tests/test_omn16930_tenant_registry_projection.py", "_envelope:ret:_envelope", 1),
    (
        "tests/test_omn17210_receipt_gate_projection_write_path.py",
        "_receipt_payload:ret:_receipt_payload",
        1,
    ),
    (
        "tests/test_omn17427_terminal_required_bar_projection.py",
        "_deployed_lane_terminal:asg:payload",
        1,
    ),
    (
        "tests/test_omn17810_pr_lifecycle_reducer_bus_projection.py",
        "<module>:asg:_FIX_COMPLETED_WIRE_PAYLOAD",
        1,
    ),
    ("tests/test_omn18768_runner_fleet_projection.py", "_runner:ret:_runner", 1),
    (
        "tests/test_omn18769_lab_lane_health_projection.py",
        "census_event:ret:census_event",
        1,
    ),
    (
        "tests/test_omn18769_lab_lane_health_projection.py",
        "health_event:ret:health_event",
        1,
    ),
    (
        "tests/test_omn18769_lab_lane_health_projection.py",
        "receipt_event:ret:receipt_event",
        1,
    ),
    ("tests/test_omn18889_local_attempt_ladder_seam.py", "_attempt:ret:_attempt", 1),
    (
        "tests/test_omn18889_real_postgres_escalation_count_write_path.py",
        "_terminal_payload:ret:_terminal_payload",
        1,
    ),
    (
        "tests/test_omn18889_real_postgres_escalation_count_write_path.py",
        "test_the_terminal_row_builder_persists_the_escalation_count:asg:payload",
        1,
    ),
    (
        "tests/test_omn18900_dod_verdict_projection.py",
        "<module>:asg:CAPTURED_PAYLOAD",
        1,
    ),
    ("tests/test_omn18903_ci_attempt_outcome_projection.py", "_check:ret:_check", 1),
    ("tests/test_omn18903_ci_attempt_outcome_projection.py", "_event:ret:_event", 1),
    (
        "tests/test_omn19448_terminal_cause_projection_real_postgres.py",
        "_skill_failed_payload:asg:payload",
        1,
    ),
    (
        "tests/test_omn19448_terminal_fields_projection_real_postgres.py",
        "test_quality_gate_refused_terminal_fields_are_copied_to_postgres:asg:payload",
        1,
    ),
    ("tests/test_omn19566_lab_proof_receipts_projection.py", "event:ret:event", 1),
    (
        "tests/test_omn20755_routing_tier_projection_real_postgres.py",
        "_attempt:ret:_attempt",
        1,
    ),
    (
        "tests/test_projection_probe_envelope_writes_row.py",
        "_build_probe_payload:ret:_build_probe_payload",
        1,
    ),
    (
        "tests/unit/nodes/node_projection_consumer_flow/test_omn17215_ac6_gap_row_does_not_shadow_observed.py",
        "_heartbeat_with_gap:ret:_heartbeat_with_gap",
        1,
    ),
    (
        "tests/unit/nodes/node_projection_delegation/test_canonical_savings_counterfactual_omn20303.py",
        "_canonical:ret:_canonical",
        1,
    ),
    (
        "tests/unit/nodes/node_projection_delegation/test_canonical_savings_counterfactual_omn20303.py",
        "_shutdown:ret:_shutdown",
        1,
    ),
    (
        "tests/unit/nodes/node_projection_hook_ledger/test_hook_ledger_event_model_binding_omn18092.py",
        "test_live_record_validated_into_declared_model_reaches_handle_intact:asg:payload",
        1,
    ),
    (
        "tests/unit/nodes/node_projection_hook_ledger/test_hook_ledger_projection_omn17201.py",
        "_envelope_record:asg:payload",
        1,
    ),
    (
        "tests/unit/nodes/node_projection_hook_ledger/test_hook_ledger_projection_omn17201.py",
        "test_real_unwrap_preserves_underscored_domain_field_for_content_identity:asg:payload",
        1,
    ),
    (
        "tests/unit/nodes/node_projection_hook_ledger/test_hook_ledger_projection_omn17201.py",
        "test_real_unwrap_preserves_underscored_domain_field_for_content_identity:asg:wire_envelope",
        1,
    ),
    (
        "tests/unit/nodes/node_projection_open_obligations/test_handler_projection_open_obligations.py",
        "_created:asg:payload",
        1,
    ),
    (
        "tests/unit/nodes/node_projection_open_obligations/test_obligations_view.py",
        "_view:asg:payload",
        1,
    ),
    (
        "tests/unit/nodes/node_projection_read_effect/test_read_binding_apart_from_claim_store_omn20159.py",
        "_broken_overlays:ret:_broken_overlays",
        1,
    ),
    (
        "tests/unit/nodes/node_projection_work_ledger/test_work_ledger_writer.py",
        "test_the_pure_handler_is_reachable_through_the_runtime_adapters_own_helper:asg:payload",
        1,
    ),
)


class PayloadSite(BaseModel):
    """One hand-written payload dict literal."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    path: str
    line: int
    site: str
    keys: tuple[str, ...]


def _constant_keys(node: ast.Dict) -> tuple[str, ...] | None:
    keys: list[str] = []
    for key in node.keys:
        if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
            return None
        keys.append(key.value)
    return tuple(keys)


def _payload_name(name: str) -> bool:
    return bool(_PAYLOADISH.search(name)) and not _NOT_PAYLOAD.search(name)


def _scope_name(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str:
    names: list[str] = []
    cursor: ast.AST | None = parents.get(node)
    while cursor is not None:
        if isinstance(cursor, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.append(cursor.name)
        cursor = parents.get(cursor)
    return ".".join(reversed(names)) or "<module>"


def _classify(
    node: ast.Dict, parents: dict[ast.AST, ast.AST]
) -> tuple[str, bool] | None:
    """The site label and whether it is the blocking (payload-named) tier."""
    parent = parents.get(node)
    if isinstance(parent, ast.keyword):
        label = f"kw:{parent.arg}"
        return label, parent.arg in _PAYLOAD_KWARGS
    if isinstance(parent, ast.Return):
        owner = parents.get(parent)
        while owner is not None and not isinstance(
            owner, ast.FunctionDef | ast.AsyncFunctionDef
        ):
            owner = parents.get(owner)
        if owner is None:
            return None
        # A helper that returns a dict literal is a fixture builder: `_attempt()`
        # names no payload word, and is the very form that stayed green at 8 keys.
        return f"ret:{owner.name}", not _NOT_PAYLOAD.search(owner.name)
    if isinstance(parent, ast.Assign | ast.AnnAssign):
        target = parent.targets[0] if isinstance(parent, ast.Assign) else parent.target
        if isinstance(target, ast.Name):
            return f"asg:{target.id}", _payload_name(target.id)
    return None


def find_sites(source: str, path: str) -> tuple[list[PayloadSite], list[PayloadSite]]:
    """The blocking payload sites and the report-only ambiguous sites of one file."""
    tree = ast.parse(source)
    parents = {
        child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)
    }
    blocking: list[PayloadSite] = []
    ambiguous: list[PayloadSite] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        keys = _constant_keys(node)
        if keys is None or len(keys) < MIN_KEYS:
            continue
        classified = _classify(node, parents)
        if classified is None:
            continue
        label, is_payload = classified
        site = PayloadSite(
            path=path,
            line=node.lineno,
            site=f"{_scope_name(node, parents)}:{label}",
            keys=keys,
        )
        (blocking if is_payload else ambiguous).append(site)
    return blocking, ambiguous


def in_scope_files(root: Path) -> list[Path]:
    found: set[Path] = set()
    for pattern in SCOPE_GLOBS:
        found.update(p for p in root.glob(pattern) if p.is_file())
    return sorted(found)


def check(
    root: Path,
    *,
    fence: tuple[tuple[str, str, int], ...],
    minimum: int,
) -> ModelHandwrittenEventPayloadCheckResult:
    files = in_scope_files(root)
    scanned = len(files)
    if scanned < minimum:
        return _result(
            1,
            scanned,
            0,
            [
                f"[{TAG}] FAIL (scan floor): only {scanned} file(s) found in scope "
                f"(floor {minimum}). A scan over a collapsed set proves nothing."
            ],
        )
    blocking: list[PayloadSite] = []
    ambiguous: list[PayloadSite] = []
    for path in files:
        relative = path.relative_to(root).as_posix()
        try:
            hard, soft = find_sites(path.read_text(errors="replace"), relative)
        except SyntaxError as exc:
            return _result(
                1, scanned, 0, [f"[{TAG}] FAIL (unparsable): {relative}: {exc.msg}"]
            )
        blocking.extend(hard)
        ambiguous.extend(soft)

    observed = Counter((s.path, s.site) for s in blocking)
    stale = sorted(
        (path, site, count, observed.get((path, site), 0))
        for path, site, count in fence
        if observed.get((path, site), 0) < count
    )
    if stale:
        lines = [
            f"[{TAG}] FAIL (stale fence): {len(stale)} fenced row(s) no longer match. "
            f"A fixed site still listed is how a fence rots into an exemption list. "
            f"Delete or lower these rows from _FENCED:"
        ]
        lines += [
            f"  - {path} {site}: fenced {count}, found {found}"
            for path, site, count, found in stale
        ]
        return _result(1, scanned, 0, lines)

    allowed = {(path, site): count for path, site, count in fence}
    seen: Counter[tuple[str, str]] = Counter()
    violations: list[PayloadSite] = []
    for s in blocking:
        key = (s.path, s.site)
        seen[key] += 1
        if seen[key] > allowed.get(key, 0):
            violations.append(s)
    fenced_sites = sum(allowed.values())
    soft_files = len({s.path for s in ambiguous})
    summary = (
        f"[{TAG}] {scanned} file(s) scanned, {len(violations)} violation(s), "
        f"{len(allowed)} fenced row(s) covering {fenced_sites} site(s) all still "
        f"present; report-only: {len(ambiguous)} ambiguous site(s) in {soft_files} "
        f"file(s)."
    )
    if violations:
        lines = [
            f"[{TAG}] FAIL: {len(violations)} hand-written event-payload dict(s). A "
            f"fixture that types the payload by hand stays green while the producer "
            f"changes. Derive it from the producer instead:"
        ]
        lines += [
            f"  - {s.path}:{s.line} {s.site} carries keys {', '.join(s.keys)}"
            for s in violations
        ]
        lines.append(summary)
        return _result(1, scanned, len(violations), lines)
    return _result(0, scanned, 0, [summary])


def _result(
    exit_code: int, scanned: int, violations: int, lines: list[str]
) -> ModelHandwrittenEventPayloadCheckResult:
    return ModelHandwrittenEventPayloadCheckResult(
        admitted=exit_code == 0,
        exit_code=exit_code,
        scanned_files=scanned,
        violations=violations,
        output_lines=tuple(lines),
    )


class HandlerHandwrittenEventPayloadCheck:
    def handle(
        self, request: ModelHandwrittenEventPayloadCheckRequest
    ) -> ModelHandwrittenEventPayloadCheckResult:
        return check(
            Path(request.repo_root),
            fence=_FENCED,
            minimum=DEFAULT_MIN_EXPECTED_FILES,
        )
