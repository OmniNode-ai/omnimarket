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
from omnimarket.nodes.node_pr_landing_decision_compute import decide_landing
from omnimarket.nodes.node_pr_landing_tick_compute import decide_landing_fair_share


class ProtocolTickDecider(Protocol):
    def decide(
        self, facts: ModelLandingFacts, fair_share: bool
    ) -> ModelLandingDecision: ...


class ProtocolTickWriter(Protocol):
    """The only door to a merge, an arm or a rerun. Observe and replay never open it."""

    def perform(self, action: ModelLandingAction) -> None: ...


class ProtocolTickPublisher(Protocol):
    def publish(self, topic: str, event: BaseModel) -> None: ...


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

    def perform(self, action: ModelLandingAction) -> None:
        self.performed.append(action)


class InMemoryTickPublisher:
    """Records the events it is given, with their topic."""

    def __init__(self) -> None:
        self.published: list[tuple[str, BaseModel]] = []

    def publish(self, topic: str, event: BaseModel) -> None:
        self.published.append((topic, event))


__all__: list[str] = [
    "InMemoryTickPublisher",
    "InMemoryTickWriter",
    "NodePathDecider",
    "ProtocolTickDecider",
    "ProtocolTickPublisher",
    "ProtocolTickWriter",
]
