# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Canonical topic constants for omnimarket event publication."""

from __future__ import annotations

TOPIC_LLM_CALL_COMPLETED = "onex.evt.omniintelligence.llm-call-completed.v1"  # onex-topic-allow: canonical topic registry, source of truth for all omnimarket topic constants

NODE_GENERATION_REQUESTED_TOPIC_V1 = "onex.cmd.omnimarket.node-generation-requested.v1"  # onex-topic-allow: canonical topic registry; declared in node_generation_consumer contract.yaml subscribe_topics

TASK_DELEGATED_TOPIC_V1 = "onex.evt.omniclaude.task-delegated.v1"  # onex-topic-allow: canonical topic registry; declared in node_delegation_orchestrator contract.yaml publish_topics
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
