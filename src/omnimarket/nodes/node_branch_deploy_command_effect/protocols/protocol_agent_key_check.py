# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The agent key check protocol of node_branch_deploy_command_effect."""

from __future__ import annotations

from typing import Protocol

from omnimarket.nodes.node_branch_deploy_command_effect.models.model_agent_key_check_reading import (
    ModelAgentKeyCheckReading,
)


class ProtocolAgentKeyCheck(Protocol):
    """The agent key check port the handler takes as a constructor argument; the default is the key check node."""

    async def read(self) -> ModelAgentKeyCheckReading: ...
