# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""FROZEN, monotonically non-increasing set of projection runner subclasses (OMN-18883).

``FROZEN_RUNNERS`` lists every ``BaseProjectionRunner`` subclass still standing, as
``module:Class``. It may only shrink: a class is deleted from it in the change that
converts the projection to a pure fold plus the generic write effect. A subclass
outside it, or an entry whose class is gone, fails the projection runner shape
check in CI and in pre-commit. Do not edit by adding a line.

``DEDICATED_DEPLOYMENT_RUNNERS`` lists the subset that declares no
``onex_runtime_inprocess_dispatch``: the shared runtime skips such a class, so each
consumes only while a dedicated writer Deployment owns its topics. These predate the
gate and are frozen the same way; a new runner must declare in-process dispatch.
The Deployment manifests live in another repository and are not read here.
"""

FROZEN_RUNNERS: tuple[str, ...] = (
    "omnimarket.nodes.node_projection_alert_channel_liveness.handlers.handler_alert_channel_liveness_writer:AlertChannelLivenessProjectionWriter",
    "omnimarket.nodes.node_projection_automation_liveness.handlers.handler_automation_liveness_writer:AutomationLivenessProjectionWriter",
    "omnimarket.nodes.node_projection_baselines.handlers.handler_baselines:BaselinesProjectionRunner",
    "omnimarket.nodes.node_projection_board_probe_results.handlers.handler_board_probe_results_writer:BoardProbeResultsProjectionWriter",
    "omnimarket.nodes.node_projection_ci_attempt_outcome.handlers.handler_ci_attempt_outcome_writer:CiAttemptOutcomeProjectionWriter",
    "omnimarket.nodes.node_projection_claude_hook_events.handlers.handler_claude_hook_events_writer:ClaudeHookEventsProjectionWriter",
    "omnimarket.nodes.node_projection_compaction_receipts.handlers.handler_compaction_receipts_writer:CompactionReceiptsProjectionWriter",
    "omnimarket.nodes.node_projection_consumer_flow.handlers.handler_consumer_flow_runner:ConsumerFlowProjectionWriter",
    "omnimarket.nodes.node_projection_cost_by_repo.handlers.handler_cost_by_repo:CostByRepoProjectionRunner",
    "omnimarket.nodes.node_projection_delegation.handlers.handler_delegation:DelegationProjectionRunner",
    "omnimarket.nodes.node_projection_delegation_budget_readings.handlers.handler_delegation_budget_readings_writer:DelegationBudgetReadingsProjectionWriter",
    "omnimarket.nodes.node_projection_delegation_capability_map.handlers.handler_delegation_capability_map_writer:HandlerDelegationCapabilityMapWriter",
    "omnimarket.nodes.node_projection_delegation_disposition.handlers.handler_delegation_disposition_writer:HandlerDelegationDispositionWriter",
    "omnimarket.nodes.node_projection_delegation_eval.handlers.handler_delegation_eval_writer:DelegationEvalProjectionWriter",
    "omnimarket.nodes.node_projection_dod_verdict.handlers.handler_dod_verdict_runner:DodVerdictProjectionWriter",
    "omnimarket.nodes.node_projection_hook_ledger.handlers.handler_hook_ledger_projection:HandlerHookLedgerProjection",
    "omnimarket.nodes.node_projection_infra_routing_decisions.handlers.handler_infra_routing_decisions_writer:InfraRoutingDecisionsProjectionWriter",
    "omnimarket.nodes.node_projection_instruction_eval.handlers.handler_instruction_eval:InstructionEvalProjectionRunner",
    "omnimarket.nodes.node_projection_intent_classification.handlers.handler_intent_classification:IntentClassificationProjectionRunner",
    "omnimarket.nodes.node_projection_lab_container_memory.handlers.handler_container_memory_writer:LabContainerMemoryProjectionWriter",
    "omnimarket.nodes.node_projection_lab_job.handlers.handler_lab_job_writer:LabJobProjectionWriter",
    "omnimarket.nodes.node_projection_lab_lane_health.handlers.handler_lab_lane_health_runner:LabLaneHealthProjectionWriter",
    "omnimarket.nodes.node_projection_lab_proof_receipts.handlers.handler_lab_proof_receipts_writer:LabProofReceiptsProjectionWriter",
    "omnimarket.nodes.node_projection_live_events.handlers.handler_live_events:HandlerLiveEventsProjectionRunner",
    "omnimarket.nodes.node_projection_llm_cost.handlers.handler_llm_cost:LlmCostProjectionRunner",
    "omnimarket.nodes.node_projection_local_environment.handlers.handler_local_environment_writer:HandlerLocalEnvironmentProjectionRunner",
    "omnimarket.nodes.node_projection_metering_summary.handlers.handler_metering_summary_writer:MeteringSummaryProjectionWriter",
    "omnimarket.nodes.node_projection_pattern_learning.handlers.handler_pattern_learning:PatternLearningProjectionRunner",
    "omnimarket.nodes.node_projection_pr_landing.handlers.handler_pr_landing_writer:PrLandingProjectionWriter",
    "omnimarket.nodes.node_projection_pr_state.handlers.handler_pr_state_projection:PrStateProjectionWriter",
    "omnimarket.nodes.node_projection_prod_promotion_gate.handlers.handler_prod_promotion_gate_writer:ProdPromotionGateProjectionWriter",
    "omnimarket.nodes.node_projection_provider_quota.handlers.handler_provider_quota_writer:ProviderQuotaProjectionWriter",
    "omnimarket.nodes.node_projection_receipt_gate.handlers.handler_receipt_gate:HandlerReceiptGateProjectionRunner",
    "omnimarket.nodes.node_projection_registration.handlers.handler_registration:RegistrationProjectionRunner",
    "omnimarket.nodes.node_projection_routing_decision.handlers.handler_routing_decision:RoutingDecisionProjectionRunner",
    "omnimarket.nodes.node_projection_routing_feedback.handlers.handler_routing_feedback_writer:HandlerRoutingFeedbackWriter",
    "omnimarket.nodes.node_projection_run_trace.handlers.handler_run_trace_writer:RunTraceProjectionWriter",
    "omnimarket.nodes.node_projection_runner_fleet.handlers.handler_fleet_liveness_writer:FleetLivenessProjectionWriter",
    "omnimarket.nodes.node_projection_runtime_error_fingerprints.handlers.handler_runtime_error_fingerprint_runner:RuntimeErrorFingerprintProjectionWriter",
    "omnimarket.nodes.node_projection_savings.handlers.handler_savings:SavingsProjectionRunner",
    "omnimarket.nodes.node_projection_session_content.handlers.handler_session_content:SessionContentProjectionWriter",
    "omnimarket.nodes.node_projection_session_outcome.handlers.handler_session_outcome:SessionOutcomeProjectionRunner",
    "omnimarket.nodes.node_projection_skill_executions.handlers.handler_skill_executions:SkillExecutionsProjectionRunner",
    "omnimarket.nodes.node_projection_tenant_credentials.handlers.handler_tenant_credentials_projection:HandlerTenantCredentialsProjectionRunner",
    "omnimarket.nodes.node_projection_tenant_registry.handlers.handler_tenant_registry_projection:HandlerTenantRegistryProjectionRunner",
    "omnimarket.nodes.node_projection_topic_activity.handlers.handler_topic_activity_writer:TopicActivityProjectionWriter",
    "omnimarket.nodes.node_projection_usage_by_model_day.handlers.handler_usage_by_model_day_writer:UsageByModelDayProjectionWriter",
    "omnimarket.nodes.node_projection_work_ledger.handlers.handler_work_ledger_projection:WorkLedgerProjectionWriter",
    "omnimarket.nodes.node_projection_worktree_reconcile.handlers.handler_worktree_reconcile_writer:WorktreeReconcileProjectionWriter",
)

DEDICATED_DEPLOYMENT_RUNNERS: tuple[str, ...] = (
    "omnimarket.nodes.node_projection_baselines.handlers.handler_baselines:BaselinesProjectionRunner",
    "omnimarket.nodes.node_projection_cost_by_repo.handlers.handler_cost_by_repo:CostByRepoProjectionRunner",
    "omnimarket.nodes.node_projection_delegation.handlers.handler_delegation:DelegationProjectionRunner",
    "omnimarket.nodes.node_projection_hook_ledger.handlers.handler_hook_ledger_projection:HandlerHookLedgerProjection",
    "omnimarket.nodes.node_projection_instruction_eval.handlers.handler_instruction_eval:InstructionEvalProjectionRunner",
    "omnimarket.nodes.node_projection_intent_classification.handlers.handler_intent_classification:IntentClassificationProjectionRunner",
    "omnimarket.nodes.node_projection_live_events.handlers.handler_live_events:HandlerLiveEventsProjectionRunner",
    "omnimarket.nodes.node_projection_llm_cost.handlers.handler_llm_cost:LlmCostProjectionRunner",
    "omnimarket.nodes.node_projection_local_environment.handlers.handler_local_environment_writer:HandlerLocalEnvironmentProjectionRunner",
    "omnimarket.nodes.node_projection_pattern_learning.handlers.handler_pattern_learning:PatternLearningProjectionRunner",
    "omnimarket.nodes.node_projection_receipt_gate.handlers.handler_receipt_gate:HandlerReceiptGateProjectionRunner",
    "omnimarket.nodes.node_projection_registration.handlers.handler_registration:RegistrationProjectionRunner",
    "omnimarket.nodes.node_projection_routing_decision.handlers.handler_routing_decision:RoutingDecisionProjectionRunner",
    "omnimarket.nodes.node_projection_run_trace.handlers.handler_run_trace_writer:RunTraceProjectionWriter",
    "omnimarket.nodes.node_projection_savings.handlers.handler_savings:SavingsProjectionRunner",
    "omnimarket.nodes.node_projection_session_outcome.handlers.handler_session_outcome:SessionOutcomeProjectionRunner",
    "omnimarket.nodes.node_projection_skill_executions.handlers.handler_skill_executions:SkillExecutionsProjectionRunner",
    "omnimarket.nodes.node_projection_tenant_credentials.handlers.handler_tenant_credentials_projection:HandlerTenantCredentialsProjectionRunner",
    "omnimarket.nodes.node_projection_tenant_registry.handlers.handler_tenant_registry_projection:HandlerTenantRegistryProjectionRunner",
)

#: Fewer runner subclasses seen than this means the scan is broken, not the tree clean.
MIN_RUNNERS: int = 40
