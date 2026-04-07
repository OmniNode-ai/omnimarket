"""Protocol interfaces for build loop sub-handlers.

Defines the contracts that each of the 6 sub-handler nodes must satisfy
when injected into the orchestrator. Protocol-based DI allows the
orchestrator to be tested in isolation and composed with either real
handlers (once migrated to omnimarket) or mock implementations.

Related:
    - OMN-7583: Migrate build loop orchestrator
    - OMN-7575: Build loop migration epic
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

# --- Lightweight result models for protocol return types ---


class CloseoutResult(BaseModel):
    """Result from a closeout operation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    success: bool = Field(default=True)


class VerifyResult(BaseModel):
    """Result from a verification operation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    all_critical_passed: bool = Field(default=True)


class ScoredTicket(BaseModel):
    """A ticket scored by RSD priority."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket_id: str = Field(..., description="Ticket identifier.")
    title: str = Field(default="", description="Ticket title.")
    rsd_score: float = Field(default=0.0, ge=0.0)
    priority: int = Field(default=0, ge=0)
    labels: tuple[str, ...] = Field(default_factory=tuple)
    description: str = Field(default="")
    state: str = Field(default="")


class RsdFillResult(BaseModel):
    """Result from RSD fill computation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    selected_tickets: tuple[ScoredTicket, ...] = Field(default_factory=tuple)
    total_selected: int = Field(default=0, ge=0)


class BuildTarget(BaseModel):
    """A ticket classified as buildable."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket_id: str = Field(..., description="Ticket identifier.")
    title: str = Field(default="")
    buildability: str = Field(default="auto_buildable")


class ClassifyResult(BaseModel):
    """Result from ticket classification."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    classifications: tuple[BuildTarget, ...] = Field(default_factory=tuple)


class DelegationPayload(BaseModel):
    """A delegation event to publish to the event bus."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    topic: str = Field(..., description="Target topic for delegation.")
    payload: dict[str, object] = Field(default_factory=dict)


class DispatchResult(BaseModel):
    """Result from build dispatch."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    total_dispatched: int = Field(default=0, ge=0)
    delegation_payloads: tuple[DelegationPayload, ...] = Field(default_factory=tuple)


# --- Protocols for sub-handlers ---


@runtime_checkable
class ProtocolCloseoutHandler(Protocol):
    """Protocol for the closeout effect handler."""

    async def handle(
        self,
        *,
        correlation_id: UUID,
        dry_run: bool = False,
    ) -> CloseoutResult: ...


@runtime_checkable
class ProtocolVerifyHandler(Protocol):
    """Protocol for the verification effect handler."""

    async def handle(
        self,
        *,
        correlation_id: UUID,
        dry_run: bool = False,
    ) -> VerifyResult: ...


@runtime_checkable
class ProtocolRsdFillHandler(Protocol):
    """Protocol for the RSD fill compute handler."""

    async def handle(
        self,
        *,
        correlation_id: UUID,
        scored_tickets: tuple[ScoredTicket, ...],
        max_tickets: int = 5,
    ) -> RsdFillResult: ...


@runtime_checkable
class ProtocolTicketClassifyHandler(Protocol):
    """Protocol for the ticket classify compute handler."""

    async def handle(
        self,
        *,
        correlation_id: UUID,
        tickets: tuple[ScoredTicket, ...],
    ) -> ClassifyResult: ...


@runtime_checkable
class ProtocolBuildDispatchHandler(Protocol):
    """Protocol for the build dispatch effect handler."""

    async def handle(
        self,
        *,
        correlation_id: UUID,
        targets: tuple[BuildTarget, ...],
        dry_run: bool = False,
    ) -> DispatchResult: ...
