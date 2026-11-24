# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ports of the landing tick orchestrator and their in-memory defaults (OMN-22921)."""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel

from omnimarket.models.landing_decision import (
    ModelLandingAction,
    ModelLandingDecision,
    ModelLandingFacts,
)
from omnimarket.models.work_ledger_append import ModelWorkLedgerAppendRequest
from omnimarket.nodes.node_pr_landing_decision_compute import decide_landing
from omnimarket.nodes.node_pr_landing_tick_compute import decide_landing_fair_share
from omnimarket.nodes.node_pr_landing_tick_orchestrator.models.model_tick_orchestrator import (
    ModelTickLivenessRead,
)


class ProtocolTickDecider(Protocol):
    def decide(
        self, facts: ModelLandingFacts, fair_share: bool
    ) -> ModelLandingDecision: ...


class ProtocolTickWriter(Protocol):
    """The only door to a merge, an arm or a rerun. Observe and replay never open it."""

    def perform(self, action: ModelLandingAction, facts: ModelLandingFacts) -> bool:
        """Perform one action of the tick whose facts are given; whether a write was sent."""
        ...


class ProtocolTickPublisher(Protocol):
    def publish(self, topic: str, event: BaseModel) -> None: ...


class ProtocolTickLedgerAppender(Protocol):
    """The only door to the ledger: hands one append request to the work ledger append effect (OMN-26549).

    Never called in observe or replay mode. A request that cannot be handed over raises.
    """

    def append(self, request: ModelWorkLedgerAppendRequest) -> None: ...


class ProtocolTickLivenessReader(Protocol):
    """The liveness read of one pull request head; observation only, it never changes a decision (OMN-26024)."""

    def read(self, pr: str, head_sha: str) -> ModelTickLivenessRead: ...


class NodePathDecider:
    """The node path: the decision node, with the per-repository fair share when the script used it."""

    def decide(
        self, facts: ModelLandingFacts, fair_share: bool
    ) -> ModelLandingDecision:
        return decide_landing_fair_share(facts) if fair_share else decide_landing(facts)


class InMemoryTickWriter:
    """Records every action it is asked to perform."""

    def __init__(self) -> None:
        self.performed: list[ModelLandingAction] = []

    def perform(self, action: ModelLandingAction, facts: ModelLandingFacts) -> bool:
        self.performed.append(action)
        return True


class InMemoryTickLedgerAppender:
    """Records the append requests it is given."""

    def __init__(self) -> None:
        self.appended: list[ModelWorkLedgerAppendRequest] = []

    def append(self, request: ModelWorkLedgerAppendRequest) -> None:
        self.appended.append(request)


class InMemoryTickPublisher:
    """Records the events it is given, with their topic."""

    def __init__(self) -> None:
        self.published: list[tuple[str, BaseModel]] = []

    def publish(self, topic: str, event: BaseModel) -> None:
        self.published.append((topic, event))


__all__: list[str] = [
    "InMemoryTickLedgerAppender",
    "InMemoryTickPublisher",
    "InMemoryTickWriter",
    "NodePathDecider",
    "ProtocolTickDecider",
    "ProtocolTickLedgerAppender",
    "ProtocolTickLivenessReader",
    "ProtocolTickPublisher",
    "ProtocolTickWriter",
]
