# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Reads the deploy agent's key check value from node_deploy_agent_key_check_effect (OMN-24396, OMN-24413)."""

from __future__ import annotations

from uuid import uuid4

from omnimarket.events.deploy_agent_key_check import (
    ModelDeployAgentKeyCheckRequest,
)
from omnimarket.nodes.node_branch_deploy_command_effect.models.model_agent_key_check_reading import (
    ModelAgentKeyCheckReading,
)
from omnimarket.nodes.node_deploy_agent_key_check_effect.handlers.handler_deploy_agent_key_check import (
    HandlerDeployAgentKeyCheck,
)


class AgentKeyCheckNodeReader:
    """Asks the key check node's handler for the deploy agent's key check value; the key never crosses this port."""

    def __init__(self, handler: HandlerDeployAgentKeyCheck, requested_by: str) -> None:
        self._handler = handler
        self._requested_by = requested_by

    async def read(self) -> ModelAgentKeyCheckReading:
        try:
            result = await self._handler.handle(
                ModelDeployAgentKeyCheckRequest(
                    correlation_id=uuid4(), requested_by=self._requested_by
                )
            )
        except Exception as exc:
            return ModelAgentKeyCheckReading(
                status="refused", detail=type(exc).__name__
            )
        if result.status == "served":
            return ModelAgentKeyCheckReading(status="read", key_check=result.key_check)
        return ModelAgentKeyCheckReading(status="refused", detail=result.detail)
