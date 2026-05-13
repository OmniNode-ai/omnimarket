# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-backed topic constants for the delegation orchestrator."""

from __future__ import annotations

from typing import Final

from omnibase_infra.event_bus.topic_constants import (
    TOPIC_DELEGATION_AGENT_TASK_LIFECYCLE,
    TOPIC_DELEGATION_INVOCATION_COMMAND,
)

TOPIC_ID_INVOCATION_COMMAND: Final[str] = TOPIC_DELEGATION_INVOCATION_COMMAND
TOPIC_ID_AGENT_TASK_LIFECYCLE: Final[str] = TOPIC_DELEGATION_AGENT_TASK_LIFECYCLE

__all__ = [
    "TOPIC_ID_AGENT_TASK_LIFECYCLE",
    "TOPIC_ID_INVOCATION_COMMAND",
]
