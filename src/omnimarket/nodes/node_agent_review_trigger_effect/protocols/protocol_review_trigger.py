# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Source-control facts, command delivery and a durable dispatch journal."""

from contextlib import AbstractAsyncContextManager
from typing import Protocol

from omnimarket.models.ci_red_triage import ModelCiRunFailedEvent
from omnimarket.models.model_agent_review_command import ModelAgentReviewRequest

from ..models.model_review_trigger import ModelReviewTriggerEvidence


class ProtocolReviewTriggerSource(Protocol):
    async def evidence(
        self, event: ModelCiRunFailedEvent
    ) -> tuple[ModelReviewTriggerEvidence, ...]: ...

    async def read_head(self, repo: str, pr: int) -> str: ...


class ProtocolReviewCommandPublisher(Protocol):
    async def publish(self, request: ModelAgentReviewRequest) -> None: ...


class ProtocolDispatchJournal(Protocol):
    def lock(self, command_id: str) -> AbstractAsyncContextManager[None]: ...

    def sent(self, command_id: str) -> bool: ...

    def record(self, command_id: str) -> None: ...
