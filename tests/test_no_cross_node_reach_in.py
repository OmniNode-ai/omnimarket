# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract test: no new cross-node model reach-ins (OMN-9263).

A "reach-in" is when node_A imports from node_B's internal models package.
All new shared event models must live in omnimarket.events.* — never in a
sibling node's models package.

KNOWN_VIOLATIONS below is a frozen allowlist of pre-existing reach-ins that
predate this refactor. It is NOT a free pass — these must be fixed in
follow-up tickets. Adding a new entry here requires a Linear ticket reference.

This test FAILS if any reach-in is introduced that is NOT in the allowlist.
Ledger reach-ins were removed by OMN-9263 and are not in the allowlist.

Key format (OMN-11116): "importer_module:from_module_path:ImportedSymbol"
  - importer_module: dot-separated module path of the file containing the import
  - from_module_path: the full dotted path after "from"
  - ImportedSymbol: each symbol named in the "import ..." clause
This format is stable across import line movements (unlike "file.py:lineno").
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_SRC_ROOT = Path(__file__).parent.parent / "src"

_REACH_IN_PATTERN = re.compile(
    r"from\s+omnimarket\.nodes\.(node_[^.]+)\..*models.*import",
)

# Pre-existing reach-ins that predate OMN-9263. Each must be resolved in a
# follow-up ticket. Format: "importer_module:from_module_path:ImportedSymbol".
# DO NOT add new entries here without a Linear ticket.
_KNOWN_VIOLATIONS: frozenset[str] = frozenset(
    [
        # node_baseline_compare → node_baseline_capture
        "omnimarket.nodes.node_baseline_compare.handlers.handler_baseline_compare:omnimarket.nodes.node_baseline_capture.models.model_baseline:BaselineProbeType",
        "omnimarket.nodes.node_baseline_compare.handlers.handler_baseline_compare:omnimarket.nodes.node_baseline_capture.models.model_baseline:ModelBaselineDelta",
        "omnimarket.nodes.node_baseline_compare.handlers.handler_baseline_compare:omnimarket.nodes.node_baseline_capture.models.model_baseline:ModelBaselineSnapshot",
        "omnimarket.nodes.node_baseline_compare.handlers.handler_baseline_compare:omnimarket.nodes.node_baseline_capture.models.model_baseline:ModelDbRowCountDelta",
        "omnimarket.nodes.node_baseline_compare.handlers.handler_baseline_compare:omnimarket.nodes.node_baseline_capture.models.model_baseline:ModelDbRowCountSnapshot",
        "omnimarket.nodes.node_baseline_compare.handlers.handler_baseline_compare:omnimarket.nodes.node_baseline_capture.models.model_baseline:ModelGitBranchDelta",
        "omnimarket.nodes.node_baseline_compare.handlers.handler_baseline_compare:omnimarket.nodes.node_baseline_capture.models.model_baseline:ModelGitBranchSnapshot",
        "omnimarket.nodes.node_baseline_compare.handlers.handler_baseline_compare:omnimarket.nodes.node_baseline_capture.models.model_baseline:ModelGitHubPRDelta",
        "omnimarket.nodes.node_baseline_compare.handlers.handler_baseline_compare:omnimarket.nodes.node_baseline_capture.models.model_baseline:ModelGitHubPRSnapshot",
        "omnimarket.nodes.node_baseline_compare.handlers.handler_baseline_compare:omnimarket.nodes.node_baseline_capture.models.model_baseline:ModelKafkaTopicDelta",
        "omnimarket.nodes.node_baseline_compare.handlers.handler_baseline_compare:omnimarket.nodes.node_baseline_capture.models.model_baseline:ModelKafkaTopicSnapshot",
        "omnimarket.nodes.node_baseline_compare.handlers.handler_baseline_compare:omnimarket.nodes.node_baseline_capture.models.model_baseline:ModelLinearTicketDelta",
        "omnimarket.nodes.node_baseline_compare.handlers.handler_baseline_compare:omnimarket.nodes.node_baseline_capture.models.model_baseline:ModelLinearTicketSnapshot",
        "omnimarket.nodes.node_baseline_compare.handlers.handler_baseline_compare:omnimarket.nodes.node_baseline_capture.models.model_baseline:ModelServiceHealthDelta",
        "omnimarket.nodes.node_baseline_compare.handlers.handler_baseline_compare:omnimarket.nodes.node_baseline_capture.models.model_baseline:ModelServiceHealthSnapshot",
        "omnimarket.nodes.node_baseline_compare.handlers.handler_baseline_compare:omnimarket.nodes.node_baseline_capture.models.model_baseline:ProbeSnapshotItem",
        "omnimarket.nodes.node_baseline_compare.models.__init__:omnimarket.nodes.node_baseline_capture.models.model_baseline:BaselineProbeType",
        "omnimarket.nodes.node_baseline_compare.models.__init__:omnimarket.nodes.node_baseline_capture.models.model_baseline:ModelBaselineDelta",
        "omnimarket.nodes.node_baseline_compare.models.__init__:omnimarket.nodes.node_baseline_capture.models.model_baseline:ModelBaselineSnapshot",
        "omnimarket.nodes.node_baseline_compare.models.__init__:omnimarket.nodes.node_baseline_capture.models.model_baseline:ModelDbRowCountDelta",
        "omnimarket.nodes.node_baseline_compare.models.__init__:omnimarket.nodes.node_baseline_capture.models.model_baseline:ModelDbRowCountSnapshot",
        "omnimarket.nodes.node_baseline_compare.models.__init__:omnimarket.nodes.node_baseline_capture.models.model_baseline:ModelGitBranchDelta",
        "omnimarket.nodes.node_baseline_compare.models.__init__:omnimarket.nodes.node_baseline_capture.models.model_baseline:ModelGitBranchSnapshot",
        "omnimarket.nodes.node_baseline_compare.models.__init__:omnimarket.nodes.node_baseline_capture.models.model_baseline:ModelGitHubPRDelta",
        "omnimarket.nodes.node_baseline_compare.models.__init__:omnimarket.nodes.node_baseline_capture.models.model_baseline:ModelGitHubPRSnapshot",
        "omnimarket.nodes.node_baseline_compare.models.__init__:omnimarket.nodes.node_baseline_capture.models.model_baseline:ModelKafkaTopicDelta",
        "omnimarket.nodes.node_baseline_compare.models.__init__:omnimarket.nodes.node_baseline_capture.models.model_baseline:ModelKafkaTopicSnapshot",
        "omnimarket.nodes.node_baseline_compare.models.__init__:omnimarket.nodes.node_baseline_capture.models.model_baseline:ModelLinearTicketDelta",
        "omnimarket.nodes.node_baseline_compare.models.__init__:omnimarket.nodes.node_baseline_capture.models.model_baseline:ModelLinearTicketSnapshot",
        "omnimarket.nodes.node_baseline_compare.models.__init__:omnimarket.nodes.node_baseline_capture.models.model_baseline:ModelServiceHealthDelta",
        "omnimarket.nodes.node_baseline_compare.models.__init__:omnimarket.nodes.node_baseline_capture.models.model_baseline:ModelServiceHealthSnapshot",
        "omnimarket.nodes.node_baseline_compare.models.__init__:omnimarket.nodes.node_baseline_capture.models.model_baseline:ProbeDeltaItem",
        "omnimarket.nodes.node_baseline_compare.models.__init__:omnimarket.nodes.node_baseline_capture.models.model_baseline:ProbeSnapshotItem",
        # node_build_loop_orchestrator → node_build_loop (multiple tickets pending)
        "omnimarket.nodes.node_build_loop_orchestrator.__main__:omnimarket.nodes.node_build_loop.models.model_loop_start_command:ModelLoopStartCommand",
        "omnimarket.nodes.node_build_loop_orchestrator.assemble_live:omnimarket.nodes.node_build_loop.models.model_loop_start_command:ModelLoopStartCommand",
        "omnimarket.nodes.node_build_loop_orchestrator.handlers.assemble_live:omnimarket.nodes.node_build_loop.models.model_loop_start_command:ModelLoopStartCommand",
        "omnimarket.nodes.node_build_loop_orchestrator.handlers.handler_orchestrator:omnimarket.nodes.node_build_loop.models.model_loop_state:EnumBuildLoopPhase",
        "omnimarket.nodes.node_build_loop_orchestrator.handlers.handler_orchestrator:omnimarket.nodes.node_build_loop.models.model_phase_transition_event:ModelPhaseTransitionEvent",
        "omnimarket.nodes.node_build_loop_orchestrator.models.model_loop_cycle_summary:omnimarket.nodes.node_build_loop.models.model_loop_state:EnumBuildLoopPhase",
        "omnimarket.nodes.node_build_loop_orchestrator.models.model_orchestrator_state:omnimarket.nodes.node_build_loop.models.model_loop_state:EnumBuildLoopPhase",
        "omnimarket.nodes.node_build_loop_orchestrator.models.model_phase_command_intent:omnimarket.nodes.node_build_loop.models.model_loop_state:EnumBuildLoopPhase",
        # node_ci_rerun_effect → node_merge_sweep_triage_orchestrator
        # OMN-9889: line shift from runtime ownership imports
        "omnimarket.nodes.node_ci_rerun_effect.handlers.handler_ci_rerun:omnimarket.nodes.node_merge_sweep_triage_orchestrator.models.model_triage_request:ModelCiRerunCommand",
        # node_conflict_hunk_effect → node_merge_sweep_triage_orchestrator
        "omnimarket.nodes.node_conflict_hunk_effect.handlers.handler_conflict_hunk:omnimarket.nodes.node_merge_sweep_triage_orchestrator.models.model_triage_request:ModelConflictHunkCommand",
        # OMN-10865: delegation pipeline migrated from omnibase_infra; the orchestrator
        # coordinates routing and quality-gate reducers by design. Shared models should
        # move to omnimarket.events.delegation.* in a follow-up ticket.
        "omnimarket.nodes.node_delegation_orchestrator.delegation_intent_bridge:omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_result:ModelQualityGateResult",
        "omnimarket.nodes.node_delegation_orchestrator.delegation_intent_bridge:omnimarket.nodes.node_delegation_routing_reducer.models.model_routing_decision:ModelRoutingDecision",
        "omnimarket.nodes.node_delegation_orchestrator.dispatchers.dispatcher_quality_gate_result:omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_result:ModelQualityGateResult",
        "omnimarket.nodes.node_delegation_orchestrator.dispatchers.dispatcher_routing_decision:omnimarket.nodes.node_delegation_routing_reducer.models.model_routing_decision:ModelRoutingDecision",
        "omnimarket.nodes.node_delegation_orchestrator.handlers.handler_compliance_loop:omnimarket.nodes.node_budget_policy_compute.models.model_budget_limits:ModelBudgetLimits",
        "omnimarket.nodes.node_delegation_orchestrator.handlers.handler_compliance_loop:omnimarket.nodes.node_budget_policy_compute.models.model_budget_policy_enums:EnumBudgetAction",
        "omnimarket.nodes.node_delegation_orchestrator.handlers.handler_compliance_loop:omnimarket.nodes.node_budget_policy_compute.models.model_budget_policy_enums:EnumTaskPriority",
        "omnimarket.nodes.node_delegation_orchestrator.handlers.handler_compliance_loop:omnimarket.nodes.node_budget_policy_compute.models.model_budget_policy_request:ModelBudgetPolicyRequest",
        "omnimarket.nodes.node_delegation_orchestrator.handlers.handler_compliance_loop:omnimarket.nodes.node_budget_policy_compute.models.model_budget_usage:ModelBudgetUsage",
        "omnimarket.nodes.node_delegation_orchestrator.handlers.handler_compliance_loop:omnimarket.nodes.node_schema_repair_compute.models.model_repair_request:ModelSchemaRepairRequest",
        "omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow:omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_input:ModelQualityGateInput",
        "omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow:omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_result:ModelQualityGateResult",
        "omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow:omnimarket.nodes.node_delegation_routing_reducer.models.model_routing_decision:ModelRoutingDecision",
        # node_delegation_routing_reducer → node_delegation_orchestrator
        # OMN-11061: topic-literal wiring added an import, shifting previous line numbers
        "omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing:omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request:ModelDelegationRequest",
        # node_intent_event_consumer_effect → node_intent_storage_effect
        "omnimarket.nodes.node_intent_event_consumer_effect.utils.util_event_mapper:omnimarket.nodes.node_intent_storage_effect.models:ModelIntentStorageRequest",
        # node_ledger_append_effect → node_ledger_orchestrator (command model, not event)
        "omnimarket.nodes.node_ledger_append_effect.handlers.handler_ledger_append:omnimarket.nodes.node_ledger_orchestrator.models.model_ledger_tick_command:ModelLedgerAppendCommand",
        # merge_sweep cluster reach-ins
        # OMN-9889: line shift from runtime ownership imports
        "omnimarket.nodes.node_merge_sweep_auto_merge_arm_effect.handlers.handler_auto_merge_arm:omnimarket.nodes.node_merge_sweep_triage_orchestrator.models.model_triage_request:ModelAutoMergeArmCommand",
        # node_merge_sweep_state_reducer reach-ins
        "omnimarket.nodes.node_merge_sweep_state_reducer.handlers.handler_sweep_state:omnimarket.nodes.node_ci_fix_effect.models.model_ci_fix_result:CiFixResult",
        "omnimarket.nodes.node_merge_sweep_state_reducer.handlers.handler_sweep_state:omnimarket.nodes.node_sweep_outcome_classify.models.model_sweep_outcome:EnumSweepOutcome",
        "omnimarket.nodes.node_merge_sweep_state_reducer.handlers.handler_sweep_state:omnimarket.nodes.node_sweep_outcome_classify.models.model_sweep_outcome:ModelSweepOutcomeClassified",
        "omnimarket.nodes.node_merge_sweep_state_reducer.handlers.handler_sweep_state:omnimarket.nodes.node_thread_reply_effect.models.model_thread_replied_event:ModelThreadRepliedEvent",
        "omnimarket.nodes.node_merge_sweep_state_reducer.models.model_merge_sweep_state:omnimarket.nodes.node_sweep_outcome_classify.models.model_sweep_outcome:EnumSweepOutcome",
        # OMN-10400: orchestrated merge-sweep workflow emits the existing
        # pr_polish command model until merge-sweep command models move to events.*.
        "omnimarket.nodes.node_merge_sweep_triage_orchestrator.handlers.handler_triage:omnimarket.nodes.node_pr_polish.models.model_pr_polish_start_command:ModelPrPolishStartCommand",
        # node_overnight → node_build_loop
        "omnimarket.nodes.node_overnight.handlers.handler_overnight:omnimarket.nodes.node_build_loop.models.model_loop_start_command:ModelLoopStartCommand",
        # node_pipeline_fill → node_rsd_fill_compute
        "omnimarket.nodes.node_pipeline_fill.handlers.handler_pipeline_fill:omnimarket.nodes.node_rsd_fill_compute.models.model_scored_ticket:ModelScoredTicket",
        # node_pr_lifecycle_fix_effect → node_pr_lifecycle_inventory_compute
        # OMN-9889: line shift from runtime ownership imports
        "omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.handler_admin_merge:omnimarket.nodes.node_pr_lifecycle_inventory_compute.models.model_pr_lifecycle_inventory:ModelStuckQueueEntry",
        # node_pr_lifecycle_orchestrator reach-ins (OMN-9806 keeps these as
        # temporary exceptions until shared lifecycle models move to events.*)
        "omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.handler_pr_lifecycle_orchestrator:omnimarket.nodes.node_pr_lifecycle_inventory_compute.models.model_pr_lifecycle_inventory:ModelPrInventoryInput",
        "omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.handler_pr_lifecycle_orchestrator:omnimarket.nodes.node_pr_lifecycle_triage_compute.models.model_pr_inventory_item:ModelPrInventoryItem",
        "omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.handler_pr_lifecycle_orchestrator:omnimarket.nodes.node_pr_lifecycle_merge_effect.models.model_merge_command:ModelPrMergeCommand",
        "omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.handler_pr_lifecycle_orchestrator:omnimarket.nodes.node_pr_lifecycle_fix_effect.models.model_fix_command:EnumPrBlockReason",
        "omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.handler_pr_lifecycle_orchestrator:omnimarket.nodes.node_pr_lifecycle_fix_effect.models.model_fix_command:ModelPrLifecycleFixCommand",
        # node_pr_review_bot → node_hostile_reviewer
        "omnimarket.nodes.node_pr_review_bot.models.models:omnimarket.nodes.node_hostile_reviewer.models.model_review_finding:EnumFindingCategory",
        "omnimarket.nodes.node_pr_review_bot.models.models:omnimarket.nodes.node_hostile_reviewer.models.model_review_finding:EnumFindingSeverity",
        "omnimarket.nodes.node_pr_review_bot.models.models:omnimarket.nodes.node_hostile_reviewer.models.model_review_finding:EnumReviewConfidence",
        "omnimarket.nodes.node_pr_review_bot.models.models:omnimarket.nodes.node_hostile_reviewer.models.model_review_finding:ModelReviewFinding",
        # node_rebase_effect → node_merge_sweep_triage_orchestrator
        "omnimarket.nodes.node_rebase_effect.handlers.handler_rebase:omnimarket.nodes.node_merge_sweep_triage_orchestrator.models.model_triage_request:ModelRebaseCommand",
        # node_thread_reply_effect → node_model_router
        "omnimarket.nodes.node_thread_reply_effect.handlers.handler_thread_reply:omnimarket.nodes.node_model_router.models.model_routing_request:ModelRoutingRequest",
    ]
)


def _collect_reach_ins() -> list[tuple[str, str, str, str]]:
    """Return (key, importer_node, imported_node, import_line) for each reach-in found.

    Key format: "importer_module:from_module_path:ImportedSymbol" — one entry per
    imported symbol so the key is stable across import line movements (OMN-11116).
    """
    found: list[tuple[str, str, str, str]] = []
    nodes_root = _SRC_ROOT / "omnimarket" / "nodes"

    for py_file in nodes_root.rglob("*.py"):
        rel = py_file.relative_to(nodes_root)
        owning_node = rel.parts[0]
        rel_from_src = py_file.relative_to(_SRC_ROOT)
        importer_module = str(rel_from_src).replace("/", ".").removesuffix(".py")

        file_lines = py_file.read_text(encoding="utf-8").splitlines()
        i = 0
        while i < len(file_lines):
            line = file_lines[i]
            m = _REACH_IN_PATTERN.search(line)
            if m is not None:
                imported_node = m.group(1)
                if imported_node != owning_node:
                    from_match = re.search(r"from\s+(\S+)\s+import", line)
                    if from_match:
                        from_path = from_match.group(1)
                        import_suffix = line[line.index("import") + 6 :].strip()
                        if import_suffix.startswith("("):
                            # Multi-line import: collect lines until closing paren
                            raw = ""
                            j = i
                            while j < len(file_lines):
                                raw += file_lines[j] + " "
                                if ")" in file_lines[j] and j > i:
                                    break
                                j += 1
                            paren_m = re.search(r"import\s*\(([^)]+)\)", raw)
                            if paren_m:
                                symbols = [
                                    s.strip().rstrip(",")
                                    for s in paren_m.group(1).split(",")
                                    if s.strip().rstrip(",")
                                ]
                            else:
                                symbols = ["UNKNOWN"]
                        else:
                            # Single-line import: "from X import A, B, C"
                            symbols = [
                                s.strip() for s in import_suffix.split(",") if s.strip()
                            ]

                        for sym in symbols:
                            key = f"{importer_module}:{from_path}:{sym}"
                            found.append(
                                (key, owning_node, imported_node, line.strip())
                            )
            i += 1

    return found


def test_no_new_cross_node_model_reach_ins() -> None:
    """No cross-node reach-ins outside the known pre-existing allowlist."""
    all_reach_ins = _collect_reach_ins()

    new_violations = [
        (key, importer, imported, code)
        for key, importer, imported, code in all_reach_ins
        if key not in _KNOWN_VIOLATIONS
    ]

    if not new_violations:
        return

    lines = [
        "New cross-node model reach-ins detected (move shared models to omnimarket.events.*):",
        "To add a temporary exception, add the key to _KNOWN_VIOLATIONS with a ticket reference.",
        "Key format: importer_module:from_module_path:ImportedSymbol",
    ]
    for key, importer, imported, code in new_violations:
        lines.append(f"  {key}  [{importer}] → [{imported}]  |  {code}")

    pytest.fail("\n".join(lines))


def test_known_violations_not_grown() -> None:
    """The known-violations allowlist must not grow beyond its baseline count.

    This catches anyone silently expanding the allowlist without fixing the
    underlying reach-in. The count is the source of truth; update it only
    when violations are *fixed* (count decreases) — never when adding new ones.
    """
    baseline = 80
    assert len(_KNOWN_VIOLATIONS) <= baseline, (
        f"_KNOWN_VIOLATIONS grew from {baseline} to {len(_KNOWN_VIOLATIONS)}. "
        "Fix a reach-in to reduce it — do not add new entries."
    )


def test_ledger_reach_ins_fully_removed() -> None:
    """Ledger cross-node reach-ins (fixed by OMN-9263) must not reappear."""
    all_reach_ins = _collect_reach_ins()
    ledger_violations = [
        (key, code)
        for key, importer, imported, code in all_reach_ins
        if (
            "node_ledger" in importer
            and "node_ledger" in imported
            and importer != imported
            and (
                "model_ledger_appended_event" in code
                or "model_ledger_hash_computed" in code
            )
        )
    ]
    if ledger_violations:
        lines = ["Ledger cross-node reach-ins reintroduced (OMN-9263 regression):"]
        for key, code in ledger_violations:
            lines.append(f"  {key}  |  {code}")
        pytest.fail("\n".join(lines))
