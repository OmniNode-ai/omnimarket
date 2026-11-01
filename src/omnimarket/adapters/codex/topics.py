# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Topic constants for the Codex runtime request adapter, read from node contracts."""

from omnimarket.nodes.contract_topics import codex_topic

TOPIC_CODEX_PATTERN_B_DISPATCH_COMMAND = codex_topic("pattern_b_dispatch_command")
TOPIC_CODEX_PATTERN_B_DISPATCH_COMPLETED = codex_topic("pattern_b_dispatch_completed")
TOPIC_CODEX_DELEGATE_SKILL_COMMAND = codex_topic("delegate_skill_command")
TOPIC_CODEX_DELEGATE_SKILL_COMPLETED = codex_topic("delegate_skill_completed")
TOPIC_CODEX_DELEGATE_SKILL_FAILED = codex_topic("delegate_skill_failed")
