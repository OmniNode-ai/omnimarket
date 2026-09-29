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


class ProtocolWorktreeFactsProbe(Protocol):
    def discover(
        self, command: ModelWorktreeReconcileCommand, now: datetime
    ) -> tuple[ModelWorktreeFacts, ...]: ...
    def revalidate(
        self, facts: ModelWorktreeFacts, now: datetime
    ) -> ModelWorktreeFacts: ...


class ProtocolWorktreeDecider(Protocol):
    def decide(
        self, argv: list[str], facts: tuple[ModelWorktreeFacts, ...]
    ) -> tuple[ModelWorktreeDecisionRecord, ...]: ...


class ProtocolWorktreePinner(Protocol):
    def pin(self, argv: list[str], facts: ModelWorktreeFacts) -> bool: ...


class ProtocolWorktreeRemover(Protocol):
    def remove(self, facts: ModelWorktreeFacts, now: datetime) -> None: ...


class ProtocolClock(Protocol):
    def now(self) -> datetime: ...


class ProtocolWorktreeEventPublisher(Protocol):
    def publish(self, topic: str, event: BaseModel) -> None: ...
