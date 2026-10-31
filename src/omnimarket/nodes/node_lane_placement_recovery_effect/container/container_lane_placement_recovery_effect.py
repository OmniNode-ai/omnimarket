# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one place that builds this node's handlers."""

from collections.abc import Mapping
from typing import Any

from ..handlers.handler_claude_account_limit import HandlerClaudeAccountLimit
from ..handlers.handler_codex_lane_limit import HandlerCodexLaneLimit
from ..handlers.handler_lane_engine_failover import HandlerLaneEngineFailover


def build_handler_claude_account_limit(
    runtime: Mapping[str, Any],
) -> HandlerClaudeAccountLimit:
    """Build the account limit handler with its runtime."""
    return HandlerClaudeAccountLimit(runtime)


def build_handler_lane_engine_failover(
    runtime: Mapping[str, Any],
) -> HandlerLaneEngineFailover:
    """Build the engine failover handler with its runtime."""
    return HandlerLaneEngineFailover(runtime)


def build_handler_codex_lane_limit() -> HandlerCodexLaneLimit:
    """Build the Codex lane limit handler."""
    return HandlerCodexLaneLimit()
