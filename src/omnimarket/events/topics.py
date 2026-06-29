# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Canonical topic constants for omnimarket event publication."""

from __future__ import annotations

TOPIC_LLM_CALL_COMPLETED = "onex.evt.omniintelligence.llm-call-completed.v1"  # onex-topic-allow: canonical topic registry, source of truth for all omnimarket topic constants

NODE_GENERATION_REQUESTED_TOPIC_V1 = "onex.cmd.omnimarket.node-generation-requested.v1"  # onex-topic-allow: canonical topic registry; declared in node_generation_consumer contract.yaml subscribe_topics

# OMN-13468: node-generation terminal topics. The failure terminal was missing
# from the topic registry and the projection contract, making failed runs
# invisible at GET /projection/onex.evt.omnimarket.node-generation-failed.v1
# (404 unknown_topic). Both terminals share the same payload shape
# (ModelGenerationBenchmark) and write to the generation_events table; only
# contract_passed differs (True for completed, False for failed).
NODE_GENERATION_COMPLETED_TOPIC_V1 = "onex.evt.omnimarket.node-generation-completed.v1"  # onex-topic-allow: canonical topic registry; declared in node_generation_consumer contract.yaml publish_topics + node_projection_delegation subscribe_topics (OMN-13468)
NODE_GENERATION_FAILED_TOPIC_V1 = "onex.evt.omnimarket.node-generation-failed.v1"  # onex-topic-allow: canonical topic registry; declared in node_generation_consumer contract.yaml publish_topics + node_projection_delegation subscribe_topics (OMN-13468)

# OMN-13629 (WS-F Phase 1): the legacy compat task-delegated.v1 event is no
# longer published by node_delegation_orchestrator nor subscribed by the
# delegation/savings projections — the terminal collapsed to the single
# canonical delegation-{completed,failed}.v1 pair below. The constant is retained
# only as a registry reference for the in-process RuntimeLocal discriminator and
# legacy tests; it is no longer a live wired topic.
TASK_DELEGATED_TOPIC_V1 = "onex.evt.omniclaude.task-delegated.v1"  # onex-topic-allow: canonical topic registry; legacy compat topic, no live producer/consumer after OMN-13629

# OMN-13629: canonical single delegation terminal pair. Emitted by
# node_delegation_orchestrator _emit_terminal; consumed by
# node_projection_delegation + node_projection_savings.
DELEGATION_COMPLETED_TOPIC_V1 = "onex.evt.omnibase-infra.delegation-completed.v1"  # onex-topic-allow: canonical topic registry; declared in node_delegation_orchestrator contract.yaml publish_topics (OMN-13629)
DELEGATION_FAILED_TOPIC_V1 = "onex.evt.omnibase-infra.delegation-failed.v1"  # onex-topic-allow: canonical topic registry; declared in node_delegation_orchestrator contract.yaml publish_topics (OMN-13629)
DELEGATE_SKILL_COMPLETED_TOPIC_V1 = "onex.evt.omnimarket.delegate-skill-completed.v1"  # onex-topic-allow: canonical topic registry; declared in node_delegate_skill_orchestrator contract.yaml terminal events
DELEGATE_SKILL_FAILED_TOPIC_V1 = "onex.evt.omnimarket.delegate-skill-failed.v1"  # onex-topic-allow: canonical topic registry; declared in node_delegate_skill_orchestrator contract.yaml terminal events
DELEGATION_CALL_COMPLETED_TOPIC_V1 = "onex.evt.omnimarket.delegation-call-completed.v1"  # onex-topic-allow: canonical topic registry; declared in node_llm_delegation_projection contract.yaml subscribe_topics
DELEGATION_ESCALATION_TRIGGERED_TOPIC_V1 = "onex.evt.omnimarket.delegation-escalation-triggered.v1"  # onex-topic-allow: canonical topic registry; declared in node_delegation_routing_feedback_reducer contract.yaml subscribe_topics
DELEGATION_ALL_TIERS_FAILED_TOPIC_V1 = "onex.evt.omnimarket.delegation-all-tiers-failed.v1"  # onex-topic-allow: canonical topic registry; declared in node_delegation_routing_feedback_reducer contract.yaml subscribe_topics
DELEGATION_PROJECTION_SNAPSHOT_TOPIC_V1 = "onex.evt.omnimarket.delegation-projection-snapshot.v1"  # onex-topic-allow: canonical topic registry; declared in node_llm_delegation_projection contract.yaml publish_topics
ROUTING_FEEDBACK_UPDATED_TOPIC_V1 = "onex.evt.omnimarket.routing-feedback-updated.v1"  # onex-topic-allow: canonical topic registry; declared in node_delegation_routing_feedback_reducer contract.yaml publish_topics

# GitHub PR merge event (OMN-13226). Published by the pr-merged-publisher GHA
# workflow on every repo merge; consumed by node_pr_merged_projection (T3,
# OMN-13227) which materialises the projection at
# GET /projection/onex.evt.github.pr-merged.v1 on the .201 lane (:3002).
PR_MERGED_TOPIC_V1 = "onex.evt.github.pr-merged.v1"  # onex-topic-allow: canonical topic registry; declared in node_pr_merged_projection contract.yaml subscribe_topics (OMN-13226/13227)

# OCC Evidence-Source autobind command (OMN-13317 / F1). Thin-published by the
# call-occ-autobind GHA workflow on product-PR opened/synchronize; consumed by
# node_pr_lifecycle_fix_effect, which routes it to the OccAutobindAdapter under
# the receipt_evidence_source_autobind block reason. The effect generates a
# receipt stamped with the real PR head + number, opens/syncs the OCC binding
# PR, recomputes contract_sha256 across all matching receipts, and PATCHes
# Evidence-Source: OCC#<n> back onto the product PR so occ-preflight goes green
# with zero manual edits.
OCC_AUTOBIND_COMMAND_TOPIC_V1 = "onex.cmd.omnimarket.occ-autobind.v1"  # onex-topic-allow: canonical topic registry; declared in node_pr_lifecycle_fix_effect contract.yaml subscribe_topics (OMN-13317)

# Typed FSM watchdog topics (OMN-12959). Canonical terminal-state-invariant
# vocabulary: every workflow FSM reaches a declared terminal OR trips one of
# these typed watchdogs. Consumed via omnimarket.events.watchdog, which maps the
# EnumWatchdogEventType class 1:1 to one of these topics.
TOPIC_WORKFLOW_TIMEOUT = "onex.evt.omnimarket.workflow-timeout.v1"  # onex-topic-allow: canonical typed-watchdog topic registry (OMN-12959)
TOPIC_WORKFLOW_UNROUTABLE = "onex.evt.omnimarket.workflow-unroutable.v1"  # onex-topic-allow: canonical typed-watchdog topic registry (OMN-12959)
TOPIC_WORKFLOW_STALLED = "onex.evt.omnimarket.workflow-stalled.v1"  # onex-topic-allow: canonical typed-watchdog topic registry (OMN-12959)

# Renderer capability declaration command (OMN-13131 / W5). A renderer thin-publishes
# its capability surface heartbeat onto this command topic; the sole writer
# node_renderer_capability_projection (NodeReducer) folds each declaration into the
# heartbeat-backed Renderer Capability Registry projection. The materialized
# projection (read via /projection/{topic}) is the only read authority — there is
# no in-memory registry class. The topic is the canonical source of truth for every
# Python consumer in the node path; the node's contract.yaml declares it for runtime
# wiring, and handler/model code references THIS constant (never the literal) so the
# no-hardcoded-topics gate stays green (G-E).
RENDERER_CAPABILITY_DECLARED_TOPIC_V1 = "onex.cmd.ui.renderer-capability-declared.v1"  # onex-topic-allow: canonical topic registry; declared in node_renderer_capability_projection contract.yaml subscribe_topics (OMN-13131)

# Context-ROI score event consumed by node_projection_capsule_store (OMN-12842).
# node_context_roi_compute emits this terminal event carrying the per-capsule
# effectiveness numbers; the capsule-store reducer folds each scored event into
# the durable capsule_store projection. The reducer references THESE constants
# (never the literals) so the no-hardcoded-topics gate stays green; the node's
# contract.yaml declares them for runtime wiring.
CONTEXT_ROI_SCORE_COMPLETED_TOPIC_V1 = "onex.evt.omnimarket.context-roi-score-completed.v1"  # onex-topic-allow: canonical topic registry; declared in node_context_roi_compute contract.yaml publish_topics and node_projection_capsule_store subscribe_topics (OMN-12842)
CAPSULE_STORE_APPLIED_TOPIC_V1 = "onex.evt.omnimarket.capsule-store-applied.v1"  # onex-topic-allow: canonical topic registry; declared in node_projection_capsule_store contract.yaml publish_topics (OMN-12842)

# M5 live closed-loop feedback edge (OMN-12845). The runner emits a scored
# runtime ROI row onto context-roi-runtime-row-scored.v1; the feedback-edge
# reducer (node_capsule_effectiveness_feedback_reducer) enforces attribution
# honesty: a CONTROLLED_INTERVENTION row is republished as a
# context-roi-score-completed.v1 effectiveness claim (folded onto the M2 capsule
# store, which then re-ranks M3 selection); an OBSERVATIONAL row is republished
# ONLY as a capsule-effectiveness-hypothesis.v1 and is never written as a measured
# score. Handler/model code references THESE constants (never the literals) so the
# no-hardcoded-topics gate stays green; each node's contract.yaml declares them.
CONTEXT_ROI_RUNTIME_ROW_SCORED_TOPIC_V1 = "onex.evt.omnimarket.context-roi-runtime-row-scored.v1"  # onex-topic-allow: canonical topic registry; declared in node_capsule_effectiveness_feedback_reducer contract.yaml subscribe_topics (OMN-12845)
CAPSULE_EFFECTIVENESS_HYPOTHESIS_TOPIC_V1 = "onex.evt.omnimarket.capsule-effectiveness-hypothesis.v1"  # onex-topic-allow: canonical topic registry; declared in node_capsule_effectiveness_feedback_reducer contract.yaml publish_topics (OMN-12845)

# OMN-13655: canonical redeploy bus path. The CI pipeline (or a thin agent shim)
# emits a ModelRuntimeImageBuilt onto RUNTIME_IMAGE_BUILT_TOPIC_V1 once an image
# has been pushed to the registry; the redeploy orchestrator subscribes and routes
# the event through the prod-promotion gate (replacing the prior imperative deploy
# script path). REDEPLOY_START_CMD_TOPIC_V1 is the original trigger topic exposed
# here so handler/model code references the constant (never the literal) and the
# no-hardcoded-topics gate stays green.
RUNTIME_IMAGE_BUILT_TOPIC_V1 = "onex.evt.omnimarket.runtime-image-built.v1"  # onex-topic-allow: canonical topic registry; declared in node_redeploy_orchestrator contract.yaml subscribe_topics (OMN-13655)
REDEPLOY_START_CMD_TOPIC_V1 = "onex.cmd.omnimarket.redeploy-start.v1"  # onex-topic-allow: canonical topic registry; declared in node_redeploy_orchestrator contract.yaml subscribe_topics (OMN-13655)
