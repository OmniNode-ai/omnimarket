# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Injected boundaries; no handler constructs its own collaborators."""

from datetime import datetime
from typing import Protocol

from pydantic import BaseModel

from omnimarket.events.worktree_reconcile import (
    ModelWorktreeDecisionRecord,
    ModelWorktreeFacts,
    ModelWorktreeReconcileCommand,
)
from omnimarket.handlers.rules_unit_roots import UnitRoots


class ProtocolWorktreeFactsProbe(Protocol):
    def discover(
        self, command: ModelWorktreeReconcileCommand, now: datetime
    ) -> tuple[ModelWorktreeFacts, ...]: ...
    def revalidate(
        self, facts: ModelWorktreeFacts, now: datetime
    ) -> ModelWorktreeFacts: ...


class ProtocolUnitRootReader(Protocol):
    """The paths installed units run from, read fresh on every call; raises when unreadable."""

    def read(self) -> UnitRoots: ...


class ProtocolWorktreeDecider(Protocol):
    def decide(
        self, argv: list[str], facts: tuple[ModelWorktreeFacts, ...]
    ) -> tuple[ModelWorktreeDecisionRecord, ...]: ...


class ProtocolMergedPrReader(Protocol):
    def merged_heads(self, argv: list[str], slug: str) -> frozenset[str]: ...


class ProtocolWorktreePinner(Protocol):
    def pin(self, argv: list[str], facts: ModelWorktreeFacts) -> bool: ...


class ProtocolWorktreeRemover(Protocol):
    def remove(self, facts: ModelWorktreeFacts, now: datetime) -> None: ...


class ProtocolClock(Protocol):
    def now(self) -> datetime: ...


class ProtocolWorktreeEventPublisher(Protocol):
    def publish(self, topic: str, event: BaseModel) -> None: ...
