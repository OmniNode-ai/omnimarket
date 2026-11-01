# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Topic constants for the Codex runtime request adapter.

Each topic is read from the event_bus section of the node contract that owns
it, so no topic string lives in this module: the Pattern B dispatch pair from
``node_pattern_b_broker`` and the delegate-skill command and terminals from
``node_delegate_skill_orchestrator``.
"""

from __future__ import annotations

from pathlib import Path

from omnimarket.nodes.contract_topics import contract_topic

# OMN-12443: The stability runtime broker contract is the omnimarket Pattern B
# bridge. Keep explicit deployment overrides in runtime_client.py for older
# runtime targets, but default the Codex runbook path to the active broker.
_NODES = Path(__file__).resolve().parents[2] / "nodes"
_PATTERN_B_BROKER_CONTRACT = _NODES / "node_pattern_b_broker" / "contract.yaml"
_DELEGATE_SKILL_CONTRACT = _NODES / "node_delegate_skill_orchestrator" / "contract.yaml"

TOPIC_CODEX_PATTERN_B_DISPATCH_COMMAND = contract_topic(
    _PATTERN_B_BROKER_CONTRACT, "publish_topics", ".pattern-b-dispatch.v1"
)
TOPIC_CODEX_PATTERN_B_DISPATCH_COMPLETED = contract_topic(
    _PATTERN_B_BROKER_CONTRACT, "subscribe_topics", ".pattern-b-dispatch-completed.v1"
)
TOPIC_CODEX_DELEGATE_SKILL_COMMAND = contract_topic(
    _DELEGATE_SKILL_CONTRACT, "subscribe_topics", ".delegate-skill.v1"
)
TOPIC_CODEX_DELEGATE_SKILL_COMPLETED = contract_topic(
    _DELEGATE_SKILL_CONTRACT, "publish_topics", ".delegate-skill-completed.v1"
)
TOPIC_CODEX_DELEGATE_SKILL_FAILED = contract_topic(
    _DELEGATE_SKILL_CONTRACT, "publish_topics", ".delegate-skill-failed.v1"
)
