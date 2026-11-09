# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point of the agent-prompt gh-read guard (OMN-23307)."""

from pathlib import Path

from .handlers.handler_agent_prompt_gh_read_guard import (
    HandlerAgentPromptGhReadGuard,
)
from .models.model_agent_prompt_gh_read import (
    ModelAgentPromptGhReadDecision,
    ModelAgentPromptGhReadRequest,
)


class NodeAgentPromptGhReadGuardEffect:
    contract_path = Path(__file__).with_name("contract.yaml")

    def __init__(self) -> None:
        self.handler = HandlerAgentPromptGhReadGuard()

    def handle(
        self, request: ModelAgentPromptGhReadRequest
    ) -> ModelAgentPromptGhReadDecision:
        return self.handler.handle(request)
