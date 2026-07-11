# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Regression coverage for all-node market runtime dogfood inventory."""

from __future__ import annotations

from scripts.audit.market_node_runtime_dogfood import build_report

FOCUS_NODES = {
    "node_contract_reducer",
    "node_cross_cli_originator",
    "node_e2e_orchestrator",
    "node_finding_aggregator_compute",
    "node_intent_storage_effect",
    "node_llm_delegation_routing_compute",
    "node_memory_storage_effect",
    "node_model_router",
    "node_navigation_history_reducer",
    "node_overseer_observer",
    "node_persona_builder_compute",
    "node_persona_retrieval_effect",
    "node_persona_storage_effect",
    "node_polish_task_classifier",
    "node_projection_dep_health",
    "node_rsd_fill_compute",
    "node_semantic_analyzer_compute",
    "node_similarity_compute",
    "node_ticket_classify_compute",
}

NATIVE_NON_ADDRESSABLE_NODES = {
    "node_e2e_orchestrator",
    "node_navigation_history_reducer",
    "node_projection_dep_health",
}

OMN_14151_LEGACY_ARM_SURFACES = {
    "node_auto_merge_effect",
    "node_merge_sweep_auto_merge_arm_effect",
    "node_merge_sweep_triage_orchestrator",
}


def test_market_node_runtime_dogfood_inventory_classifies_all_entry_points() -> None:
    report = build_report()
    summary = report["summary"]

    # OMN-13210 B1, OMN-13211 B3, and OMN-13212 B2 each decompose a legacy
    # workflow node into canonical nodes; B2 nets +3 (4 new canonical nodes minus
    # the deleted node_pr_review_bot shell): 308 -> 311.
    # OMN-13226 T2 adds node_pr_merged_projection stub: 311 -> 312.
    # OMN-13131 W5 adds node_renderer_capability_projection reducer: 312 -> 313.
    # OMN-13356 adds node_tool_reuse_matcher_compute: 313 -> 314.
    # OMN-12842 M2 adds node_projection_capsule_store reducer: 314 -> 315.
    # OMN-12846 adds node_user_correction_observer_effect: 315 -> 316.
    # OMN-12844 M4 adds node_context_exploration_policy_compute: 316 -> 317.
    # OMN-12843 M3 adds node_context_selection_policy_compute: 317 -> 318.
    # OMN-13385 adds node_contract_graph_ir_compute (read-only IR GET surface):
    # 318 -> 319.
    # OMN-12845 M5 adds node_capsule_effectiveness_feedback_reducer: 319 -> 320.
    # OMN-13439 Phase 2b adds node_prod_promotion_grant_resolver_effect: 320 -> 321.
    # OMN-13476 W4 extracts the delegation escalation/tier decision into
    # node_delegation_escalation_decision_compute (COMPUTE): 321 -> 322.
    # OMN-12884 adds node_projection_replay_check_compute: 322 -> 323.
    # OMN-13083 adds node_projection_traces (traces projection contract):
    # 323 -> 324.
    # OMN-13583 adds node_repo_health_classify_compute (COMPUTE; keystone of the
    # merge-sweep repo-health lane): 324 -> 325.
    # OMN-13413 adds node_runtime_closeout_orchestrator (ORCHESTRATOR; one-dispatch
    # runtime closeout, epic OMN-13410): 325 -> 326.
    # OMN-12998 adds node_projection_instruction_eval (instruction-eval aggregate
    # projection; replaces hardcoded fixture with contract-declared projection): 326 -> 327.
    # OMN-13584 adds node_repo_health_repair_effect (EFFECT; durable repo-health
    # repair ticket emission): 327 -> 328.
    # OMN-13441 Phase 1.3 adds node_prod_health_fact_resolver_effect (EFFECT;
    # un-forgeable prod-health fact for the prod-promotion gate): 328 -> 329.
    # OMN-13606 SEA Phase 0.2 adds node_generated_node_publish_effect (EFFECT;
    # auto-PR publish step of the SEA self-extension loop): 329 -> 330.
    # OMN-13614 WS-C Phase 3.1 adds node_entropy_experiment_orchestrator
    # (ORCHESTRATOR; SEA->canonical entropy experiment aggregation emitting the
    # shared core ModelExperimentResult): 330 -> 331.
    # OMN-13615 SEA Phase 3.2 adds node_model_eval_orchestrator (ORCHESTRATOR;
    # canonical model-eval experiment home migrated from SEA): 331 -> 332.
    # OMN-13616 SEA Phase 3.3 adds node_regression_test_orchestrator (ORCHESTRATOR;
    # deterministic regression replay emitting the canonical experiment result,
    # epic OMN-13604): 332 -> 333.
    # OMN-13620 WS-C Phase 5.1 adds node_projection_event_chain (REDUCER; canonical
    # replayable per-event chain projection replacing the SEA event-chain ledger,
    # epic OMN-13604): 333 -> 334.
    # OMN-12809 retires node_dispatch_request_handler: 334 -> 333.
    # OMN-13075 adds node_projection_baselines_roi: 333 -> 334.
    # OMN-13076 NC-03 adds node_projection_baselines_quality (REDUCER; quality
    # snapshot projection for the omnidash quality-baseline-panel widget): 334 -> 335.
    # OMN-13723 adds node_slack_publish_effect (EFFECT; generic secret-store-backed
    # Slack publish primitive for the morning deep-dive skill epic): 335 -> 336.
    # OMN-13724 adds node_report_format_compute (COMPUTE; md+metrics -> Slack
    # Block Kit payload for the morning-report pipeline): 336 -> 337.
    # OMN-13725 adds node_deep_dive_report_effect (EFFECT; git/gh/Linear I/O owner
    # for the daily deep-dive report): 337 -> 338.
    # OMN-13080 NC-07 adds node_projection_mcp_tools (REDUCER; MCP tools snapshot
    # projection for the omnidash mcp-tools widget): 338 -> 339.
    # OMN-13078 NC-05 adds node_projection_intent_classification (REDUCER;
    # session-timeline + intent-distribution projection): 339 -> 340.
    # OMN-13087 adds node_projection_session_replay (REDUCER; session replay
    # snapshot projection for the omnidash Session Replay widget): 340 -> 341.
    # OMN-13081 adds node_projection_receipt_gate (REDUCER; OCC/DoD
    # receipt-gate projection for the NC-08 dashboard widget): 341 -> 342.
    # OMN-13079 NC-06 adds node_projection_live_events (REDUCER; live-events
    # stream projection contract for the omnidash live-event-stream widget):
    # 342 -> 343.
    # OMN-13086 adds node_projection_voice_sessions (REDUCER; voice session
    # projection for the omnidash voice.sessions widget): 343 -> 344.
    # OMN-13085 NC-12 adds node_projection_sandbox_decisions (REDUCER; sandbox
    # decisions projection contract for the omnidash sandbox-decisions widget):
    # 344 -> 345.
    # OMN-13088 NC-15 adds node_projection_delegation_inference_response
    # (REDUCER; inference-response-text projection for the omnidash delegation
    # model-output widget): 345 -> 346.
    # OMN-13839 adds node_projection_skill_executions (REDUCER; skill-lifecycle
    # snapshot projection completing the measurement pipeline emit -> table ->
    # snapshot topic -> skill-adoption widget): 346 -> 347.
    # OMN-13859 adds node_pr_lifecycle_worktree_prune_effect (EFFECT; event-driven
    # worktree prune-on-PR-close driven by pr_lifecycle_orchestrator): 347 -> 348.
    # OMN-13925 adds node_env_parity_collect_effect (EFFECT; live read-only
    # runtime-lane env collection over ssh + parity evaluation, the live front-end
    # for the env_parity skill): 348 -> 349.
    # OMN-13940 adds node_pr_delegated_fix_effect (EFFECT; WS-D/D2 merge-sweep
    # delegation harness Slice 0 -- deterministic ruff-fix path re-entering
    # the existing pr_polish gate/verify/push flow): 349 -> 350.
    # OMN-14285 adds node_occ_companion_compute (COMPUTE; pure deterministic
    # OCC companion planning and attestation oracle for RSD-1): 350 -> 351.
    # OMN-14333 adds node_generated_code_validator and node_mypy_check_effect:
    # 351 -> 353.
    # OMN-14325 adds node_contract_serialize_compute plus four pure compute
    # leaves for compliant model-to-contract serialization: 353 -> 358.
    # OMN-14336 adds node_hybrid_codegen_orchestrator,
    # node_llm_codegen_effect, and node_codegen_file_writer_effect: 358 -> 361.
    # OMN-14326 adds node_ast_node_analyzer and node_stub_detector pure
    # compute nodes for codegen analysis: 361 -> 363.
    # OMN-14307 adds node_github_repo_gateway_effect (EFFECT; typed read-only
    # GitHub repo status gateway for merge-sweep verification): 363 -> 364.
    # OMN-14151 adds node_pr_arm_gate_compute (COMPUTE; fail-closed ARM/WITHHOLD
    # decider for the merge-queue governor): 364 -> 365.
    assert summary["node_dirs"] == 365
    # OMN-14151 deliberately removes request/response entry points from the
    # three legacy arm surfaces; the new arm-gate compute node is the single
    # active route.
    assert summary["entry_points"] == 362
    assert set(summary["missing_entry_points"]) == OMN_14151_LEGACY_ARM_SURFACES
    assert summary["dangling_entry_points"] == []
    assert summary["routable"] >= 299
    assert summary["skipped"] == 4
    assert summary["failed"] == 0
    assert summary["failure_buckets"] == {}
    assert {
        item["node_name"]
        for item in report["skipped"]
        if item["bucket"] == "non_addressable"
    } == {
        "node_e2e_orchestrator",
        "node_navigation_history_reducer",
        "node_projection_dep_health",
        "node_pr_merged_projection",  # OMN-13226 T2 stub; handler pending T3
    }
    assert not {
        item["node_name"]
        for item in report["skipped"]
        if item["node_name"].endswith("_compute")
    }


def test_market_node_runtime_dogfood_proves_nested_contract_shapes() -> None:
    report = build_report()
    routable = {item["node_name"]: item for item in report["routable"]}

    assert (
        routable["node_ab_compare_orchestrator"]["input_model"]
        == "omnimarket.nodes.node_ab_compare_orchestrator.models.model_ab_compare_start.ModelAbCompareStart"
    )
    assert (
        routable["node_loop_state_reducer"]["handler"]
        == "omnimarket.nodes.node_loop_state_reducer.handlers.handler_loop_state.HandlerLoopState"
    )
    assert (
        routable["node_adr_canary_orchestrator"]["input_model"]
        == "omnimarket.nodes.node_adr_canary_orchestrator.models.model_canary_request.ModelCanaryCommandPayload"
    )
    assert (
        routable["node_session_compose"]["command_topic"]
        == "onex.cmd.omnimarket.session-compose.v1"
    )
    assert (
        routable["node_ticket_query"]["terminal_topic"]
        == "onex.evt.omnimarket.ticket-query-completed.v1"
    )
    assert (
        routable["node_dirty_canonical_sweep"]["command_topic"]
        == "onex.cmd.omnimarket.dirty-canonical-sweep.v1"
    )
    assert (
        routable["node_code_embedding_effect"]["input_model"]
        == "omnimarket.nodes.node_code_embedding_effect.models.model_code_embedding_request.ModelCodeEmbeddingRequest"
    )
    assert (
        routable["node_emit_daemon"]["input_model"]
        == "omnimarket.nodes.node_emit_daemon.models.model_daemon_state.ModelEmitDaemonCommand"
    )
    assert (
        routable["node_emit_daemon"]["command_topic"]
        == "onex.cmd.omnimarket.emit-daemon-lifecycle.v1"
    )
    assert (
        routable["node_similarity_compute"]["command_topic"]
        == "onex.cmd.omnimemory.similarity-compute.v1"
    )
    assert (
        routable["node_projection_replay_check_compute"]["command_topic"]
        == "onex.cmd.omnimarket.projection-replay-check-start.v1"
    )
    assert (
        routable["node_projection_replay_check_compute"]["terminal_topic"]
        == "onex.evt.omnimarket.projection-replay-check-completed.v1"
    )
    assert (
        routable["node_projection_replay_check_compute"]["input_model"]
        == "omnimarket.nodes.node_projection_replay_check_compute.models.model_replay_check.ModelReplayCheckRequest"
    )
    assert (
        routable["node_projection_replay_check_compute"]["handler"]
        == "omnimarket.nodes.node_projection_replay_check_compute.handlers.handler_projection_replay_check.HandlerProjectionReplayCheck"
    )
    assert (
        routable["node_memory_storage_effect"]["command_topic"]
        == "onex.cmd.omnimemory.memory-storage.v1"
    )
    assert (
        routable["node_overseer_observer"]["input_model"]
        == "omnimarket.nodes.node_overseer_observer.models.model_overseer_observation_request.ModelOverseerObservationRequest"
    )


def test_market_node_runtime_dogfood_proves_handler_key_repairs() -> None:
    report = build_report()
    routable = {item["node_name"]: item for item in report["routable"]}

    assert (
        routable["node_agent_coordinator_orchestrator"]["handler"]
        == "omnimemory.handlers.handler_subscription.HandlerSubscription"
    )
    assert (
        routable["node_memory_lifecycle_orchestrator"]["handler"]
        == "omnimarket.nodes.node_memory_lifecycle_orchestrator.handlers.handler_memory_tick.HandlerMemoryTick"
    )
    assert (
        routable["node_persona_lifecycle_orchestrator"]["handler"]
        == "omnimarket.nodes.node_persona_lifecycle_orchestrator.handlers.handler_persona_rebuild.HandlerPersonaRebuild"
    )


def test_market_node_runtime_dogfood_audits_former_non_addressable_nodes() -> None:
    report = build_report()
    routable = {item["node_name"]: item for item in report["routable"]}
    skipped = {item["node_name"]: item for item in report["skipped"]}
    failures = {item["node_name"]: item for item in report["failures"]}

    assert failures.keys().isdisjoint(FOCUS_NODES)
    assert (set(routable) | set(skipped)) & FOCUS_NODES == FOCUS_NODES
    assert set(skipped) & FOCUS_NODES == NATIVE_NON_ADDRESSABLE_NODES

    command_addressable_nodes = FOCUS_NODES - NATIVE_NON_ADDRESSABLE_NODES
    assert command_addressable_nodes <= set(routable)
    assert all(
        routable[node_name]["command_topic"].startswith("onex.cmd.")
        for node_name in command_addressable_nodes
    )
