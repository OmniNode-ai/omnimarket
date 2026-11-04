# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ports of the agent review dispatch. The handler takes each by injection and
reads no file, clock, environment or network of its own."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from omnimarket.nodes.node_agent_review_dispatch_orchestrator.models.model_agent_review import (
    ModelAgentReceipt,
    ModelAgentReceiptCandidate,
    ModelAgentReviewTerminal,
    ModelAgentVoter,
    ModelAgentVoterReview,
)


class ProtocolAgentReviewHead(Protocol):
    """Source-control read: the head sha a pull request has now."""

    async def read_head(self, repo: str, pr: int) -> str: ...


class ProtocolAgentReviewLab(Protocol):
    """One lab call through the delegation product, for one voter."""

    async def review(
        self, voter: ModelAgentVoter, repo: str, pr: int, head_sha: str
    ) -> ModelAgentVoterReview: ...


class ProtocolAgentReviewSigner(Protocol):
    """The signer node: signs a candidate under the node's key."""

    async def sign(
        self, candidate: ModelAgentReceiptCandidate
    ) -> ModelAgentReceipt: ...


class ProtocolAgentReceiptStore(Protocol):
    """One immutable receipt per key ``repo#pr#head``, written by atomic insert-if-absent."""

    async def get(self, key: str) -> ModelAgentReceipt | None: ...

    async def insert_if_absent(
        self, key: str, receipt: ModelAgentReceipt
    ) -> ModelAgentReceipt:
        """Store ``receipt`` unless the key holds one; return what the key holds."""
        ...


class ProtocolAgentReviewPoster(Protocol):
    """The source-control write ``pr_review_post`` and the read that finds a posted receipt."""

    async def find_posted(
        self, repo: str, pr: int, publisher_identity: str, receipt_id: str
    ) -> bool: ...

    async def post(
        self, repo: str, pr: int, head_sha: str, receipt: ModelAgentReceipt, fence: str
    ) -> None: ...


class ProtocolAgentReviewTerminalLog(Protocol):
    """The terminal recorded per command id."""

    async def get(self, command_id: str) -> ModelAgentReviewTerminal | None: ...

    async def record(self, terminal: ModelAgentReviewTerminal) -> None: ...


class ProtocolAgentReviewClock(Protocol):
    """The nonce and the time of a candidate receipt."""

    def now(self) -> datetime: ...

    def new_id(self) -> str: ...

    def monotonic(self) -> float: ...


__all__ = [
    "ProtocolAgentReceiptStore",
    "ProtocolAgentReviewClock",
    "ProtocolAgentReviewHead",
    "ProtocolAgentReviewLab",
    "ProtocolAgentReviewPoster",
    "ProtocolAgentReviewSigner",
    "ProtocolAgentReviewTerminalLog",
]
