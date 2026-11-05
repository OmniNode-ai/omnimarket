# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed routing dependencies: admission is owned by core, dispatch by the existing fill contract."""

from __future__ import annotations

from typing import Protocol

from omnimarket.models.model_lane_routing import (
    ModelFillBacklogCommand,
    ModelLaneBlockFacts,
    ModelOwnerDiagnosis,
    ModelOwnerMap,
    ModelTriageCommand,
)


class ProtocolLaneRoutingGateway(Protocol):
    def diagnose(self, facts: ModelLaneBlockFacts) -> ModelOwnerDiagnosis:
        """Reach the pure diagnosis node through its declared bus contract."""
        ...

    def admit(
        self, command: ModelFillBacklogCommand, owners: ModelOwnerMap
    ) -> str | None:
        """Core admission's refusal, or None when admitted; never implement policy here."""
        ...

    def fill(self, command: ModelFillBacklogCommand) -> str:
        """Return the owner lane; retries of command.key MUST return the same lane."""
        ...

    def bind(self, key: str, lane: str, items: tuple[str, ...]) -> None:
        """Idempotently bind every item to the shared owner lane."""
        ...

    def exception(self, key: str, evidence: str, reason: str) -> None:
        """Emit an idempotent exception row to the existing exception queue."""
        ...

    def ticket_repository(self, ticket: str) -> str | None:
        """Read the ticket's authoritative repository, with no row/default fallback."""
        ...

    def triage(self, request: ModelTriageCommand) -> str:
        """Start an idempotent read-only triage lane without a worker or code permission."""
        ...
