# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one place that builds this node's handlers."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..adapters.handler_coding_agent_bus import HandlerCodingAgentBus


def build_handler_coding_agent_bus() -> HandlerCodingAgentBus:
    """Build the bus adapter lazily to avoid its discovery handler import cycle."""
    from ..adapters.handler_coding_agent_bus import HandlerCodingAgentBus

    return HandlerCodingAgentBus()
