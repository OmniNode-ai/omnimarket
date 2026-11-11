# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""R-B ports: read live facts, call core validation, and use Part A's claim store."""

from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Protocol, runtime_checkable

from omnimarket.models.model_rule_premise import (
    ModelBatchSlot,
    ModelBatchState,
    ModelBriefCheckRequest,
    ModelBriefCheckResult,
    ModelBriefPremise,
    ModelPremiseRead,
    ModelPremiseValidationRequest,
)


@runtime_checkable
class ProtocolRulePremiseGate(Protocol):
    def handle(self, request: ModelBriefCheckRequest) -> ModelBriefCheckResult: ...


@runtime_checkable
class ProtocolPremiseReader(Protocol):
    def read(self, premise: ModelBriefPremise, host: str) -> ModelPremiseRead: ...


@runtime_checkable
class ProtocolPremiseValidator(Protocol):
    """Core owns validation, including brief shape, read freshness and receipt authority."""

    def check(
        self, request: ModelPremiseValidationRequest
    ) -> ModelBriefCheckResult: ...

    def read_back(self, receipt_id: str, premise: str, brief_id: str) -> bool:
        """Validate a durable read-back for this precise premise and brief, never a checkpoint."""
        ...


class ProtocolBatchTransaction(Protocol):
    def read(self) -> ModelBatchState | None: ...

    def insert_slot(self, slot: ModelBatchSlot) -> bool:
        """One unique-key insert, batch@<id>#<n>; never delete a spent slot."""
        ...

    def write(self, state: ModelBatchState) -> None:
        """Atomically persist the fold and its escalation outbox, keyed by state.escalation.

        Replaying a held state must create no second escalation. The deployment's
        existing Part A reaper re-offers the original brief when its lease expires.
        """
        ...


@runtime_checkable
class ProtocolBatchClaimStore(Protocol):
    def transaction(
        self, batch: str
    ) -> AbstractContextManager[ProtocolBatchTransaction]:
        """Serializable across both fill paths, also fencing terminal and proof writes."""
        ...


PremiseGateFactory = Callable[[], ProtocolRulePremiseGate]
