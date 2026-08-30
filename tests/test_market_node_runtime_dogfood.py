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
    # OMN-14608 adds node_codegen_outcome_reducer (REDUCER; joins the three raw
    # codegen downstream verdicts to retained pipeline state so the hybrid
    # codegen orchestrator's outcome topics have a real producer): 365 -> 366.
    # OMN-14619 adds node_occ_state_effect (EFFECT; read-only OCC companion
    # state gatherer for the RSD producer chain): 366 -> 367.
    # OMN-14622 adds node_occ_companion_effect (EFFECT; deterministic OCC companion
    # write-effect + orchestrator for the RSD producer chain): 367 -> 368.
    # OMN-14648 adds node_merge_state_projection (REDUCER; report-only merge-flow
    # telemetry projection): 368 -> 369.
    # OMN-14393 adds node_occ_autoauthor_window (COMPUTE; N=10 report-only
    # auto-authoring observation counter) and node_occ_attestation_observe
    # (EFFECT; read-only report-only companion attestation gate): 369 -> 371.
    # OMN-14726 adds node_delivery_replay_projection_compute (COMPUTE; B6
    # deterministic delivery/replay projection checksum + cursor tool): 371 -> 372.
    # OMN-14735 adds node_canary_monitoring_gate_compute (COMPUTE; B10
    # monitoring-signal-to-threshold gate scaffold, thresholds pending A6): 372 -> 373.
    # OMN-14851 adds node_occ_observation_projection (COMPUTE; storage-agnostic
    # dedup projection scaffold for the OCC N=10 real-doneness counter): 373 -> 374.
    # OMN-14888 adds node_occ_observation_effect (EFFECT; durable append-only OCC
    # observation write, dry_run default) and node_occ_observation_source_effect
    # (EFFECT; reads the durable OCC observation trail from a checkout and feeds
    # the existing dedup projection): 374 -> 376.
    # OMN-14920 adds node_push_validation_effect (EFFECT; hook-verified,
    # suite-gated, fail-closed branch push for the gateway push-validation
    # workflow — closes the zero-consumer window on the #624 command topic):
    # 376 -> 377.
    # OMN-14977 adds node_worker_memory_admission_compute (COMPUTE; D3
    # RAM-aware worker admission — headroom formula + fail-closed staleness
    # gate, no live bus wiring yet): 377 -> 378.
    # OMN-14978 adds node_fleet_partition_key_compute (COMPUTE; fleet
    # topology keying — deterministic injective repo:branch partition key,
    # no live bus wiring yet): 378 -> 379.
    # OMN-15126 adds node_liveness_demand_query_effect (EFFECT; real Postgres
    # demand-source query + correlated join, design OMN-14845 §3.2 steps 1-2)
    # and node_liveness_evaluate_compute (COMPUTE; pure demand-aware liveness
    # state decision — NOT_READY/NO_DEMAND/HEALTHY/STALE/RED, design §3.2 —
    # no live bus wiring yet, directly-invoked only): 379 -> 381.
    # OMN-15164 adds node_report_anchor_probe_effect (EFFECT; content-anchor
    # git-SHA/artifact-path/PR-number re-probes feeding the OMN-15163
    # report-validation COMPUTE node — no live bus wiring yet, directly-invoked
    # only, same runtime_dispatch-only pattern as node_liveness_demand_query_effect):
    # 381 -> 382.
    # OMN-15163 adds node_report_validation_compute (COMPUTE; deterministic
    # shape + content-anchor validation of dispatch-worker report payloads
    # against the OMN-15161 report contract, consuming OMN-15164's probe
    # output — no live bus wiring yet, directly-invoked only): 382 -> 383.
    # OMN-15253 adds node_staging_readiness_compute (COMPUTE; pure fail-closed
    # evaluation of a caller-supplied staging snapshot against the typed
    # staging-composition contract — zero I/O, no live bus wiring yet,
    # directly-invoked only, same pattern as node_report_validation_compute):
    # 383 -> 384.
    # OMN-15763 adds node_seam_graph_compute (COMPUTE; manifest-driven
    # seam-graph extractor — contract-declared `seams:` blocks + code-level
    # producer/consumer/env/@ref scan, no live bus wiring yet, directly
    # invoked via runtime_dispatch.command_topic) and node_seam_match_compute
    # (COMPUTE; canonical seam-projection/v1 serializer + three-leg
    # seam-match classifier, same directly-invoked pattern): 384 -> 386.
    # OMN-15983 deletes node_multi_agent_orchestrator (dead code: both bus
    # directions ORPHANED, the same-named /onex:multi_agent CLI skill never
    # dispatches to it, and its only handler-implementation ticket OMN-12329
    # is Canceled with no standing intent to wire): 386 -> 385.
    # OMN-15965 adds node_event_emit_effect (EFFECT_GENERIC; R1 of the
    # node_emit_daemon replacement — thin-publish-to-Kafka with a file-based
    # spool outbox, direct def-B CLI/plugin-runtime dispatch, no live bus
    # wiring yet): 385 -> 386.
    # OMN-16090 adds node_hook_event_capture (REDUCER_GENERIC; consumes the
    # gateway's hook-event-capture command topic and persists each carried
    # event into hook_events, idempotent on (tenant_id, event_sha) — the cloud
    # half of the path that drains an operator machine's stranded emit spool.
    # Its catalog entry lands FENCED on the gateway side, so there is no live
    # traffic on the command topic yet): 386 -> 387.
    # OMN-16191 deletes node_doc_freshness_sweep. It never had an implementation
    # of its own — it called onex_change_control's scanner functions behind a
    # try/except ImportError whose fallback returned status="error" with every
    # count defaulting to zero, indistinguishable from a clean sweep. Product
    # code no longer depends on the governance repo, which left the node with no
    # implementation at all rather than a degraded one: 387 -> 386.
    # OMN-16316 adds node_projection_tenant_credentials (REDUCER; BYOK
    # inference-credential-ref ingress+projection — consumes the gateway
    # value->ref thin-publisher's credential-registered/credential-revoked
    # events and materializes tenant_inference_credentials, the only writer
    # to that table per OMN-15800): 386 -> 387.
    # OMN-16777 adds node_projection_consumer_flow (REDUCER; Phase 1 of the
    # platform-observability epic OMN-16776 — consumes the flow_window the
    # runtime heartbeat now carries and derives FLOWING/STALLED/STARVED/IDLE/
    # UNKNOWN per (consumer_group, topic, window). It is the first surface that
    # measures throughput across a seam rather than connectedness, which is why
    # a consumer at LAG 0 with 15,750 in and 0 out read as healthy): 387 -> 388.
    # OMN-16778 adds node_consumer_flow_stall_alert_effect (EFFECT; the other
    # half of the same phase -- a projection nobody reads is not observability,
    # so this node turns a confirmed STALLED/STARVED run into a Slack alert
    # naming the consumer, the topic and the counts): 388 -> 389.
    # OMN-15600 adds node_alert_channel_liveness_effect (EFFECT; the last open
    # item on the same phase's gate -- an alert that was delivered at 05:27Z
    # proves the channel was alive at 05:27Z and nothing about 05:28Z, so this
    # node re-proves it on the existing heartbeat and classifies a channel that
    # cannot deliver as DEAD / NOT_CONFIGURED / PROBE_ERROR rather than letting
    # an HTTP 200 carrying {"ok": false} read as success): 389 -> 390.
    # 391 as of OMN-16180: node_projection_work_events, the L1 work-ledger
    # projection over the four live omniclaude hook topics.
    # OMN-17202 adds node_hook_chain_probe_effect (EFFECT; the union proof over
    # the same hook chain -- every ticket on it proved its own leg and nothing
    # proved the whole, so it was green-by-parts and dead-in-fact. It emits one
    # correlated hook event and reports the furthest leg it reached, naming the
    # allowlist denial / lane mismatch / non-relay transport rather than timing
    # out): 391 -> 392.
    # OMN-16930 adds node_projection_tenant_registry (REDUCER; the tenant
    # registry mirror -- consumes the registry-owned tenant lifecycle events
    # and materializes omninode_internal.tenant_registry_mirror, the
    # cross-tenant slug<->uuid index the OMN-16930 registry-resolved
    # conversion reads to convert migration 0031's legacy slug-keyed rows to
    # canonical tenant uuid at apply time): 392 -> 393.
    # OMN-17019 adds node_projection_open_obligations, the materialized
    # "what is currently owed" fold over the five work.obligation.* events.
    assert summary["node_dirs"] == 394
    # OMN-14151 deliberately removes request/response entry points from the
    # three legacy arm surfaces; the new arm-gate compute node is the single
    # active route. OMN-14608's reducer entry point brings the count back up:
    # 362 -> 363. OMN-14619 adds the state-effect gather route: 363 -> 364.
    # OMN-14622 adds the companion write-effect route: 364 -> 365.
    # OMN-14648 adds the merge-state projection route: 365 -> 366.
    # OMN-14393 adds the window (compute) + attestation-observe (effect) routes:
    # 366 -> 368.
    # OMN-14726 adds the delivery-replay-projection compute route (addressable via
    # runtime_dispatch, resolves as routable): 368 -> 369.
    # OMN-14735 adds the canary-monitoring-gate compute route (addressable via
    # runtime_dispatch, resolves as routable): 369 -> 370.
    # OMN-14851 adds the observation-projection compute route (addressable via
    # runtime_dispatch, resolves as routable): 370 -> 371.
    # OMN-14888 adds the observation-effect (write) and observation-source-effect
    # (read) routes (both addressable via runtime_dispatch, resolve as
    # routable): 371 -> 373.
    # OMN-14920 adds the push-validation write-effect route (addressable via
    # runtime_dispatch, resolves as routable): 373 -> 374.
    # OMN-14977 adds the worker-memory-admission compute route (addressable
    # via runtime_dispatch, resolves as routable — same no-live-bus-yet
    # pattern as node_canary_monitoring_gate_compute): 374 -> 375.
    # OMN-14978 adds the fleet-partition-key compute route (addressable via
    # runtime_dispatch, resolves as routable): 375 -> 376.
    # OMN-15126 adds the liveness-demand-query-effect and
    # liveness-evaluate-compute routes (both addressable via runtime_dispatch,
    # resolve as routable — same no-live-bus-yet pattern as
    # node_fleet_partition_key_compute): 376 -> 378.
    # OMN-15164 adds the report-anchor-probe-effect route (addressable via
    # runtime_dispatch, resolves as routable): 378 -> 379.
    # OMN-15163 adds the report-validation-compute route (addressable via
    # runtime_dispatch, resolves as routable): 379 -> 380.
    # OMN-15253 adds the staging-readiness-compute route (addressable via
    # runtime_dispatch, resolves as routable): 380 -> 381.
    # OMN-15763 adds the seam-graph-compute and seam-match-compute routes
    # (both addressable via runtime_dispatch.command_topic, resolve as
    # routable): 381 -> 383.
    # OMN-15983 removes the node_multi_agent_orchestrator entry point
    # (dead code deletion, see node_dirs comment above): 383 -> 382.
    # OMN-15965 adds the node_event_emit_effect entry point (see node_dirs
    # comment above): 382 -> 383.
    # OMN-16090 adds the node_hook_event_capture entry point (see node_dirs
    # comment above): 383 -> 384.
    # OMN-16191 removes the node_doc_freshness_sweep entry point along with the
    # node (see node_dirs comment above): 384 -> 383.
    # OMN-16316 adds the node_projection_tenant_credentials entry point (see
    # node_dirs comment above; routable via its command_topic
    # onex.evt.omnimarket.credential-registered.v1): 383 -> 384.
    # OMN-16777 adds the node_projection_consumer_flow entry point (see
    # node_dirs comment above; routable via its subscribe topic
    # onex.evt.platform.node-heartbeat.v1): 384 -> 385.
    # OMN-16778 adds the node_consumer_flow_stall_alert_effect entry point
    # (routable via its subscribe topic
    # onex.evt.omnimarket.projection-consumer-flow-applied.v1): 385 -> 386.
    # OMN-15600 adds the node_alert_channel_liveness_effect entry point
    # (routable via its subscribe topic onex.evt.platform.node-heartbeat.v1 —
    # the carrier the observability epic names, so the check dies with the
    # runtime it measures instead of polling a corpse): 386 -> 387.
    # OMN-16180 adds the node_projection_work_events entry point (see node_dirs
    # comment above; routable via its subscribe topics, the four live omniclaude
    # hook topics -- session-started, prompt-submitted, tool-executed,
    # session-ended): 387 -> 388.
    # OMN-17202 adds the node_hook_chain_probe_effect entry point (see node_dirs
    # comment above; routable via its runtime_dispatch.command_topic): 388 -> 389.
    # OMN-16930 adds the node_projection_tenant_registry entry point (see
    # node_dirs comment above; routable via its subscribe topic
    # onex.tenant.events, the control-plane tenant lifecycle topic owned by
    # onex-api): 389 -> 390.
    # OMN-17019 adds the node_projection_open_obligations entry point (see the
    # node_dirs comment above; routable via its subscribe topics, the five
    # work.obligation.* fan-out topics): 390 -> 391.
    assert summary["entry_points"] == 391
    assert set(summary["missing_entry_points"]) == OMN_14151_LEGACY_ARM_SURFACES
    assert summary["dangling_entry_points"] == []
    assert summary["routable"] >= 299
    # OMN-14648's report-only projection is non-addressable: 4 -> 5.
    assert summary["skipped"] == 5
    assert summary["failed"] == 0
    assert summary["failure_buckets"] == {}
    assert {
        item["node_name"]
        for item in report["skipped"]
        if item["bucket"] == "non_addressable"
    } == {
        "node_e2e_orchestrator",
        "node_merge_state_projection",
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
