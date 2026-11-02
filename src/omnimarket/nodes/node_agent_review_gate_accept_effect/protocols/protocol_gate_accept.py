# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ports the gate-accept node is given: files, reviews, verifier and the bus."""

from collections.abc import Awaitable, Callable
from typing import Protocol

from omnibase_infra.event_bus.models.model_event_message import ModelEventMessage

from omnimarket.nodes.node_agent_review_gate_accept_effect.models.model_gate_accept import (
    ModelReceiptVerifyOutcome,
    ModelReceiptVerifyRequest,
    ModelReviewView,
)


class ProtocolStepFiles(Protocol):
    """The step's files: this attempt's voter result, the step output and summary."""

    def read_text(self, path: str) -> str: ...

    def append_text(self, path: str, text: str) -> None: ...


class ProtocolReviewReader(Protocol):
    """The pull request's reviews, read through the source-control node."""

    def reviews(self, repository: str, pull_request: int) -> list[ModelReviewView]: ...


class ProtocolReceiptVerifier(Protocol):
    """The verifier node (compute), called once per receipt-bearing review."""

    def verify(
        self, request: ModelReceiptVerifyRequest
    ) -> ModelReceiptVerifyOutcome: ...


class ProtocolGateAcceptBus(Protocol):
    """The bus the decision travels over: the in-memory or the Redpanda binding."""

    async def publish(self, topic: str, key: bytes | None, value: bytes) -> object: ...

    async def subscribe_ungrouped(
        self,
        topic: str,
        on_message: Callable[[ModelEventMessage], Awaitable[None]],
    ) -> Callable[[], Awaitable[None]]: ...
