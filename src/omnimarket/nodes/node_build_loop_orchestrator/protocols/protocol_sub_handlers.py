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


class CloseoutRequest(BaseModel):
    """Typed request payload for the closeout effect handler (def-B).

    Structurally mirrors ``ModelCloseoutInput`` at the orchestrator seam. The
    real ``HandlerCloseout`` (cast to this protocol) reads only
    ``request.correlation_id`` / ``request.dry_run``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Build loop cycle correlation ID.")
    dry_run: bool = Field(default=False, description="Skip actual side effects.")


class VerifyRequest(BaseModel):
    """Typed request payload for the verification effect handler (def-B).

    Structurally mirrors ``ModelVerifyInput`` at the orchestrator seam. The
    real ``HandlerVerify`` (cast to this protocol) reads only
    ``request.correlation_id`` / ``request.dry_run``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Build loop cycle correlation ID.")
    dry_run: bool = Field(default=False, description="Skip actual checks.")


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


class RsdFillRequest(BaseModel):
    """Typed request payload for the RSD fill compute handler (def-B).

    Structurally mirrors ``ModelRsdFillInput`` at the orchestrator seam
    (``correlation_id`` + ``scored_tickets`` + ``max_tickets``) without reaching
    into the RSD fill node's private model package. The real ``HandlerRsdFill``
    (cast to this protocol) reads only ``request.correlation_id`` /
    ``request.scored_tickets`` / ``request.max_tickets``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Build loop cycle correlation ID.")
    scored_tickets: tuple[ScoredTicket, ...] = Field(
        default_factory=tuple, description="Pre-scored candidate tickets."
    )
    max_tickets: int = Field(default=5, ge=0, description="Maximum tickets to select.")


class BuildTarget(BaseModel):
    """A ticket classified as buildable."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket_id: str = Field(..., description="Ticket identifier.")
    title: str = Field(default="")
    buildability: str = Field(default="auto_buildable")
    template_node_id: str | None = Field(
        default=None,
        description="Template node to use for code generation context (FSM vs compute).",
    )


class ClassifyResult(BaseModel):
    """Result from ticket classification."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    classifications: tuple[BuildTarget, ...] = Field(default_factory=tuple)


class ClassifyRequest(BaseModel):
    """Typed request payload for the ticket classify compute handler (def-B).

    Structurally mirrors ``ModelTicketClassifyInput`` at the orchestrator seam
    (``correlation_id`` + ``tickets``) without reaching into the classify node's
    private model package. The real ``HandlerTicketClassify`` (cast to this
    protocol) reads only ``request.correlation_id`` / ``request.tickets``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Build loop cycle correlation ID.")
    tickets: tuple[ScoredTicket, ...] = Field(
        default_factory=tuple, description="Tickets to classify."
    )


class DelegationPayload(BaseModel):
    """A delegation event to publish to the event bus."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    topic: str = Field(..., description="Target topic for delegation.")
    payload: dict[str, object] = Field(default_factory=dict)


class DispatchRequest(BaseModel):
    """Typed request payload for the build dispatch effect handler (def-B).

    Structurally mirrors ``ModelBuildDispatchInput`` at the orchestrator seam
    (``correlation_id`` + ``targets`` + ``dry_run``) without reaching into the
    dispatch node's private model package. The real ``HandlerBuildDispatch``
    (cast to this protocol) reads only ``request.correlation_id`` /
    ``request.targets`` / ``request.dry_run``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Build loop cycle correlation ID.")
    targets: tuple[BuildTarget, ...] = Field(
        default_factory=tuple, description="Tickets to dispatch for building."
    )
    dry_run: bool = Field(default=False, description="Skip actual dispatch.")


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
        request: CloseoutRequest,
    ) -> CloseoutResult: ...


@runtime_checkable
class ProtocolVerifyHandler(Protocol):
    """Protocol for the verification effect handler."""

    async def handle(
        self,
        request: VerifyRequest,
    ) -> VerifyResult: ...


@runtime_checkable
class ProtocolRsdFillHandler(Protocol):
    """Protocol for the RSD fill compute handler (def-B, typed payload)."""

    async def handle(
        self,
        request: RsdFillRequest,
    ) -> RsdFillResult: ...


@runtime_checkable
class ProtocolTicketClassifyHandler(Protocol):
    """Protocol for the ticket classify compute handler (def-B, typed payload)."""

    async def handle(
        self,
        request: ClassifyRequest,
    ) -> ClassifyResult: ...


@runtime_checkable
class ProtocolBuildDispatchHandler(Protocol):
    """Protocol for the build dispatch effect handler."""

    async def handle(
        self,
        request: DispatchRequest,
    ) -> DispatchResult: ...
