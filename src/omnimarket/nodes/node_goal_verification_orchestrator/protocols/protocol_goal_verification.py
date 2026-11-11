# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Injected dependencies of the goal closure orchestrator (OMN-20033)."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from omnimarket.models.model_dod_verify_start_command import (
    ModelDodVerifyStartCommand,
)
from omnimarket.nodes.node_goal_verification_orchestrator.models.model_goal_verification import (
    ModelGoalClosure,
    ModelGoalContractRevision,
    ModelGoalVerificationRun,
)


class ProtocolGoalVerificationStore(Protocol):
    """Durable record of runs. Every method is atomic; a real backend must survive a crash."""

    def record_close(
        self, closure: ModelGoalClosure
    ) -> tuple[ModelGoalVerificationRun, bool]:
        """Create the run PENDING, or add the closing event id to the existing run.

        Returns the run and whether this call created it.
        """
        ...

    def get_run(
        self, goal_id: UUID, contract_revision: UUID
    ) -> ModelGoalVerificationRun | None: ...

    def mark_attempt(
        self, goal_id: UUID, contract_revision: UUID, at: datetime
    ) -> ModelGoalVerificationRun:
        """Count one published start command and move the run to STARTED."""
        ...

    def mark_unread_attempt(
        self, goal_id: UUID, contract_revision: UUID, at: datetime
    ) -> ModelGoalVerificationRun:
        """Count an attempt that could not read the goal contract; the run stays PENDING."""
        ...

    def mark_verdict(
        self, goal_id: UUID, contract_revision: UUID, verifier_correlation_id: UUID
    ) -> ModelGoalVerificationRun: ...

    def mark_exhausted(
        self, goal_id: UUID, contract_revision: UUID, reason: str
    ) -> ModelGoalVerificationRun: ...

    def record_disposition(
        self, goal_id: UUID, contract_revision: UUID, event_id: UUID
    ) -> bool:
        """Remember a disposition. Never creates a run. Returns False for a repeat."""
        ...

    def list_unverified(self) -> tuple[ModelGoalVerificationRun, ...]:
        """Every run in PENDING, STARTED or EXHAUSTED state."""
        ...


class ProtocolGoalContractReader(Protocol):
    """Reads the goal contract the verifier checks."""

    def read(
        self, goal_id: UUID, contract_revision: UUID
    ) -> ModelGoalContractRevision | None: ...

    def find_by_delegation_correlation(
        self, delegation_correlation_id: UUID
    ) -> ModelGoalContractRevision | None:
        """The call goal a delegation run belongs to, or None when it has no goal."""
        ...


class ProtocolVerifierStartPublisher(Protocol):
    """Publishes the verifier start command durably. Raises when it could not publish."""

    def publish(self, command: ModelDodVerifyStartCommand) -> None: ...


__all__: list[str] = [
    "ProtocolGoalContractReader",
    "ProtocolGoalVerificationStore",
    "ProtocolVerifierStartPublisher",
]
