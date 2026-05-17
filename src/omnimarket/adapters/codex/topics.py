# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Topic constants for the Codex runtime request adapter."""

# OMN-11065: Aligned to the omnibase-infra topic namespace that the .201 runtime
# consumers are active on. The previous omnimarket-namespaced topics were unconsumed.
TOPIC_CODEX_PATTERN_B_DISPATCH_COMMAND = "onex.cmd.omnibase-infra.pattern-b-dispatch.v1"  # onex-topic-allow: pending contract auto-wiring
TOPIC_CODEX_PATTERN_B_DISPATCH_COMPLETED = "onex.evt.omnibase-infra.pattern-b-dispatch-completed.v1"  # onex-topic-allow: pending contract auto-wiring
TOPIC_CODEX_DELEGATE_SKILL_COMMAND = "onex.cmd.omnimarket.delegate-skill.v1"  # onex-topic-allow: pending contract auto-wiring
