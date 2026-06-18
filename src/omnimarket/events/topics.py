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

# Typed FSM watchdog topics (OMN-12959). Canonical terminal-state-invariant
# vocabulary: every workflow FSM reaches a declared terminal OR trips one of
# these typed watchdogs. Consumed via omnimarket.events.watchdog, which maps the
# EnumWatchdogEventType class 1:1 to one of these topics.
TOPIC_WORKFLOW_TIMEOUT = "onex.evt.omnimarket.workflow-timeout.v1"  # onex-topic-allow: canonical typed-watchdog topic registry (OMN-12959)
TOPIC_WORKFLOW_UNROUTABLE = "onex.evt.omnimarket.workflow-unroutable.v1"  # onex-topic-allow: canonical typed-watchdog topic registry (OMN-12959)
TOPIC_WORKFLOW_STALLED = "onex.evt.omnimarket.workflow-stalled.v1"  # onex-topic-allow: canonical typed-watchdog topic registry (OMN-12959)
