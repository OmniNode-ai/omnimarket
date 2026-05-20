# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Canonical topic constants for omnimarket event publication."""

from __future__ import annotations

TOPIC_LLM_CALL_COMPLETED = "onex.evt.omniintelligence.llm-call-completed.v1"  # onex-topic-allow: canonical topic registry, source of truth for all omnimarket topic constants

TASK_DELEGATED_TOPIC_V1 = "onex.evt.omniclaude.task-delegated.v1"  # onex-topic-allow: canonical topic registry; declared in node_delegation_orchestrator contract.yaml publish_topics
DELEGATE_SKILL_COMPLETED_TOPIC_V1 = "onex.evt.omnimarket.delegate-skill-completed.v1"  # onex-topic-allow: canonical topic registry; declared in node_delegate_skill_orchestrator contract.yaml terminal events
DELEGATE_SKILL_FAILED_TOPIC_V1 = "onex.evt.omnimarket.delegate-skill-failed.v1"  # onex-topic-allow: canonical topic registry; declared in node_delegate_skill_orchestrator contract.yaml terminal events
