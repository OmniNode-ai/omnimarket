# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_projection_claude_hook_events -- every Claude Code hook, with lineage.

Projects ``onex.evt.omniclaude.hook-event.v1`` (one topic for all 33 hook
types) into ``omninode_internal.claude_hook_events`` and folds subagent lineage
into ``omninode_internal.claude_agent_spans``.

The package re-exports the pure fold and its models, which is this node's
wiring evidence for the unimported-handler check. The writer is deliberately
NOT re-exported: it imports the Kafka projection-runner stack, and importing
the package for the pure fold must not drag that in.
"""

from omnimarket.nodes.node_projection_claude_hook_events.handlers.handler_projection_claude_hook_events import (
    HandlerProjectionClaudeHookEvents,
    resolve_parent,
)
from omnimarket.nodes.node_projection_claude_hook_events.models import (
    EnumClaudeHookEventName,
    EnumParentResolution,
    ModelClaudeHookEventWire,
    ModelClaudeHookProjectionRequest,
    ModelClaudeHookProjectionResult,
)

__all__: list[str] = [
    "EnumClaudeHookEventName",
    "EnumParentResolution",
    "HandlerProjectionClaudeHookEvents",
    "ModelClaudeHookEventWire",
    "ModelClaudeHookProjectionRequest",
    "ModelClaudeHookProjectionResult",
    "resolve_parent",
]
