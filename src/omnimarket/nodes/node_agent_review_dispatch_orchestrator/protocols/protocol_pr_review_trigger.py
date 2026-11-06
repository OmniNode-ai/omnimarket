# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Source-control read and review-command publication on the canonical bus."""

from typing import Any, Protocol

from pydantic import BaseModel

from omnimarket.models.scm import EnumScmNode

from ..models.model_agent_review import ModelAgentReviewRequest


class ProtocolPrReviewTriggerBus(Protocol):
    async def call(self, node: EnumScmNode, request: BaseModel) -> dict[str, Any]: ...

    async def publish_review(self, request: ModelAgentReviewRequest) -> None: ...
