# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Durable per-item lifecycle record for the legacy dispatch queue (OMN-17018).

The record is append-only: every transition is kept, so "what happened to this
item" is replayable rather than inferred from the last write. Expiry marks a
claim stale; it never rewrites history and never deletes the queue item.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.enums.enum_dispatch_queue_phase import (
    IN_FLIGHT_PHASES,
    EnumDispatchQueuePhase,
)
from omnimarket.enums.enum_dispatch_terminal_reason import (
    EnumDispatchTerminalDisposition,
    EnumDispatchTerminalReason,
)


class ModelDispatchQueueTerminal(BaseModel):
    """How and why an item reached ``TERMINAL``.

    ``COMPLETED`` must carry no reason and ``STOPPED`` must carry one — the
    reason taxonomy describes stops, and a nullable reason that silently means
    "finished fine" is exactly the untyped ``result: null`` this ticket removes.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    disposition: EnumDispatchTerminalDisposition
    reason: EnumDispatchTerminalReason | None = None

    @model_validator(mode="after")
    def validate_reason_matches_disposition(self) -> ModelDispatchQueueTerminal:
        if self.disposition is EnumDispatchTerminalDisposition.STOPPED:
            if self.reason is None:
                raise ValueError("a stopped lane must carry a terminal reason")
        elif self.reason is not None:
            raise ValueError("a completed lane must not carry a stop reason")
        return self

    @property
    def auto_redispatchable(self) -> bool:
        """Whether recovery policy may auto-redispatch this terminal outcome."""
        if self.reason is None:
            return False
        return self.reason.auto_redispatchable


class ModelDispatchQueueTransition(BaseModel):
    """One durable lifecycle transition for one queue item."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    phase: EnumDispatchQueuePhase
    occurred_at: datetime
    actor: str = Field(..., min_length=1, description="Who performed the transition.")
    detail: str = Field(default="", description="Human-readable transition note.")
    #: Renewable claim lease. Past it the claim is STALE — never deleted, never
    #: silently reset to QUEUED (operator ruling A1-REVISED).
    lease_expires_at: datetime | None = None
    #: Deadline by which the dispatched lane must acknowledge that it started.
    #: Past it the item is observably PENDING.
    ack_deadline: datetime | None = None
    terminal: ModelDispatchQueueTerminal | None = None

    @model_validator(mode="after")
    def validate_terminal_only_on_terminal_phase(self) -> ModelDispatchQueueTransition:
        is_terminal_phase = self.phase is EnumDispatchQueuePhase.TERMINAL
        if is_terminal_phase and self.terminal is None:
            raise ValueError("a TERMINAL transition must carry a terminal disposition")
        if not is_terminal_phase and self.terminal is not None:
            raise ValueError("only a TERMINAL transition may carry a disposition")
        return self


class ModelDispatchQueueLifecycle(BaseModel):
    """Append-only lifecycle history for one queue item."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    queue_item_path: str = Field(..., min_length=1)
    item_id: str = Field(..., min_length=1, description="Queue file stem.")
    transitions: tuple[ModelDispatchQueueTransition, ...] = Field(..., min_length=1)

    @property
    def latest(self) -> ModelDispatchQueueTransition:
        """The most recent transition. Never empty — ``transitions`` is min_length=1."""
        return self.transitions[-1]

    @property
    def phase(self) -> EnumDispatchQueuePhase:
        """Current phase of the item."""
        return self.latest.phase

    @property
    def terminal(self) -> ModelDispatchQueueTerminal | None:
        """Terminal disposition when the item is finished, else ``None``."""
        return self.latest.terminal

    def is_in_flight(self) -> bool:
        """Whether an attempt currently holds this item."""
        return self.phase in IN_FLIGHT_PHASES

    def is_stale(self, now: datetime) -> bool:
        """Whether an in-flight claim lease has expired without being renewed.

        A stale item is marked, not reclaimed: it stays in its phase and stays
        out of the selectable set until something explicitly acts on it.
        """
        lease = self.latest.lease_expires_at
        if lease is None or not self.is_in_flight():
            return False
        return now >= lease

    def is_pending_acknowledgement(self, now: datetime) -> bool:
        """Whether a dispatched item never acknowledged that it started.

        True the moment the item is DISPATCHED and stays true until either an
        acknowledgement moves it to STARTED or something records a TERMINAL.
        A timed-out acknowledgement is *visibly* pending, never counted as
        processed and never re-selected as untouched.
        """
        return self.phase is EnumDispatchQueuePhase.DISPATCHED

    def acknowledgement_timed_out(self, now: datetime) -> bool:
        """Whether the acknowledgement deadline for a DISPATCHED item has passed."""
        if self.phase is not EnumDispatchQueuePhase.DISPATCHED:
            return False
        deadline = self.latest.ack_deadline
        if deadline is None:
            return False
        return now >= deadline


__all__: list[str] = [
    "ModelDispatchQueueLifecycle",
    "ModelDispatchQueueTerminal",
    "ModelDispatchQueueTransition",
]
