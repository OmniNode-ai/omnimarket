# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2025 OmniNode Team
"""Handler for RuntimeTick - memory lifecycle TTL evaluation.

This handler processes RuntimeTick events from the runtime scheduler
and evaluates memory entities for TTL expiration and archive eligibility.
It queries the memory lifecycle projection for entities that need lifecycle
transition events emitted.

Detection Logic:
    For Memory Expiration:
        - Query projection for ACTIVE memories with expires_at <= now
        - Use projection.needs_expiration_event() for deduplication
        - Emit ModelMemoryExpiredEvent for each expired memory

    For Archive Initiation:
        - Query projection for EXPIRED memories with archived_at IS NULL
        - Use projection.needs_archive_event() for deduplication
        - Emit ModelMemoryArchiveInitiated for each archive candidate

Deduplication:
    The projection stores emission markers (expiration_emitted_at,
    archive_initiated_at) to prevent duplicate lifecycle events.
    The projection reader filters out already-emitted transitions.

Coroutine Safety:
    This handler is stateless and coroutine-safe for concurrent calls
    with different tick instances.

Container-Driven Pattern:
    This handler follows the ONEX container-driven initialization pattern:
    - Constructor takes only ModelONEXContainer
    - Dependencies are injected via initialize()
    - health_check() and describe() provide introspection

Related Tickets:
    - OMN-1453: OmniMemory P4b - Lifecycle Orchestrator Database Integration
    - OMN-1524: Infra projection reader primitives (future dependency)
    - OMN-1577: Refactor to container-driven pattern
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import TYPE_CHECKING, Protocol
from uuid import UUID, uuid4

from omnibase_core.enums import EnumMessageCategory, EnumNodeKind
from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput
from omnimemory.enums import EnumLifecycleState
from omnimemory.utils.concurrency import CircuitBreaker, CircuitBreakerOpenError
from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from omnibase_core.container import ModelONEXContainer
    from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
    from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

logger = logging.getLogger(__name__)

# Timeout for projection reader calls (seconds)
_PROJECTION_READER_TIMEOUT_SECONDS: float = 10.0


# =============================================================================
# EVENT MODELS (Placeholder - will be extracted to separate module)
# =============================================================================
class ModelMemoryExpiredEvent(BaseModel):  # omnimemory-model-exempt: handler event
    """Event emitted when a memory entity's TTL has expired.

    This event signals that a memory has transitioned from ACTIVE to EXPIRED
    state due to its TTL being reached. Downstream handlers should update
    projections and potentially initiate archival.

    Attributes:
        entity_id: The memory entity that expired.
        memory_id: Alias for entity_id (memory-specific semantic).
        correlation_id: Correlation ID for distributed tracing.
        causation_id: The tick_id that triggered this expiration.
        emitted_at: Timestamp when this event was emitted (from tick.now).
        expires_at: The original expiration deadline that was exceeded.
        lifecycle_revision: Current revision for optimistic locking.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    entity_id: UUID = Field(..., description="The memory entity that expired.")
    memory_id: UUID = Field(..., description="The memory ID (alias for entity_id).")
    correlation_id: UUID = Field(..., description="Correlation ID for tracing.")
    causation_id: UUID = Field(..., description="The tick_id that triggered this.")
    emitted_at: datetime = Field(..., description="When this event was emitted.")
    expires_at: datetime = Field(..., description="The original expiration deadline.")
    lifecycle_revision: int = Field(
        default=1, ge=1, description="Revision for optimistic locking."
    )


class ModelMemoryArchiveInitiated(BaseModel):  # omnimemory-model-exempt: handler event
    """Event emitted when archival is initiated for an expired memory.

    This event signals that a memory in EXPIRED state should be moved to
    archive storage. The actual archival is performed by the archive effect
    handler which subscribes to this event.

    Attributes:
        entity_id: The memory entity to archive.
        memory_id: Alias for entity_id (memory-specific semantic).
        correlation_id: Correlation ID for distributed tracing.
        causation_id: The tick_id that triggered this archival.
        emitted_at: Timestamp when this event was emitted (from tick.now).
        expired_at: When the memory transitioned to EXPIRED state.
        lifecycle_revision: Current revision for optimistic locking.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    entity_id: UUID = Field(..., description="The memory entity to archive.")
    memory_id: UUID = Field(..., description="The memory ID (alias for entity_id).")
    correlation_id: UUID = Field(..., description="Correlation ID for tracing.")
    causation_id: UUID = Field(..., description="The tick_id that triggered this.")
    emitted_at: datetime = Field(..., description="When this event was emitted.")
    expired_at: datetime | None = Field(
        default=None, description="When the memory transitioned to EXPIRED."
    )
    lifecycle_revision: int = Field(
        default=1, ge=1, description="Revision for optimistic locking."
    )


# =============================================================================
# RESULT MODEL
# =============================================================================


class ModelMemoryTickResult(BaseModel):  # omnimemory-model-exempt: handler result
    """Result of memory lifecycle tick evaluation.

    Captures metrics and identifiers from a single tick evaluation cycle.
    Used for observability and debugging of lifecycle processing.

    Attributes:
        expired_count: Number of memories transitioned to EXPIRED state.
        archive_initiated_count: Number of archive jobs initiated.
        tick_id: The tick that triggered this evaluation.
        sequence_number: The tick's sequence number for ordering.
        evaluated_at: Timestamp of evaluation (from tick.now).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    expired_count: int = Field(
        default=0, ge=0, description="Number of memories expired this tick."
    )
    archive_initiated_count: int = Field(
        default=0, ge=0, description="Number of archive jobs started this tick."
    )
    tick_id: UUID = Field(..., description="The tick that triggered this evaluation.")
    sequence_number: int = Field(
        ..., ge=0, description="Tick sequence for ordering and restart detection."
    )
    evaluated_at: datetime = Field(
        ..., description="Timestamp of evaluation (from injected now)."
    )


class ModelMemoryTickHealth(BaseModel):  # omnimemory-model-exempt: handler health
    """Health status for the Memory Tick Handler.

    Returned by health_check() to provide detailed health information
    about the handler and its dependencies.

    Attributes:
        initialized: Whether the handler has been initialized.
        circuit_breaker_state: Current state of the circuit breaker.
        projection_reader_available: Whether a projection reader is configured.
        batch_size: Configured batch size for tick processing.
    """

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
    )

    initialized: bool = Field(
        ...,
        description="Whether the handler has been initialized",
    )
    circuit_breaker_state: str | None = Field(
        default=None,
        description="Current state of the circuit breaker (closed, open, half_open)",
    )
    projection_reader_available: bool = Field(
        ...,
        description="Whether a projection reader is configured",
    )
    batch_size: int = Field(
        ...,
        ge=1,
        description="Configured batch size for tick processing",
    )


class ModelMemoryTickMetadata(BaseModel):  # omnimemory-model-exempt: handler metadata
    """Metadata describing memory tick handler capabilities and configuration.

    Returned by describe() method to provide introspection information
    about the handler's purpose, capabilities, and message types.

    Attributes:
        name: Handler class name.
        description: Brief description of handler purpose.
        capabilities: List of supported capabilities.
        initialized: Whether the handler has been initialized.
        message_types: Set of message types this handler processes.
        node_kind: The node kind this handler belongs to.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    name: str = Field(
        ...,
        description="Handler class name",
    )
    description: str = Field(
        ...,
        description="Brief description of handler purpose",
    )
    capabilities: list[str] = Field(
        ...,
        description="List of supported capabilities",
    )
    initialized: bool = Field(
        ...,
        description="Whether the handler has been initialized",
    )
    message_types: list[str] = Field(
        ...,
        description="Message types this handler processes",
    )
    node_kind: str = Field(
        ...,
        description="The node kind this handler belongs to",
    )


# =============================================================================
# PROJECTION READER PROTOCOL (Placeholder for OMN-1524)
# =============================================================================


class ProtocolMemoryLifecycleProjectionReader(Protocol):
    """Protocol for reading memory lifecycle projection state.

    This protocol defines the interface for querying memory entities
    that are candidates for lifecycle transitions. Implementations
    will be provided by OMN-1524 (Infra Projection Reader Primitives).

    Note:
        This is a placeholder protocol. The actual implementation will
        use omnibase_infra projection reader primitives when available.
    """

    async def get_expired_candidates(
        self,
        now: datetime,
        domain: str,
        correlation_id: UUID,
        limit: int = 100,
    ) -> list[ModelMemoryLifecycleProjection]:
        """Get ACTIVE memories that have passed their TTL.

        Query: lifecycle_state = 'active' AND expires_at IS NOT NULL AND expires_at <= :now
               AND expiration_emitted_at IS NULL

        Args:
            now: Current time for deadline comparison.
            domain: Domain scope for the query.
            correlation_id: Correlation ID for tracing.
            limit: Maximum number of results to return.

        Returns:
            List of projections for memories eligible for expiration.
        """
        ...

    async def get_archive_candidates(
        self,
        now: datetime,
        domain: str,
        correlation_id: UUID,
        limit: int = 100,
    ) -> list[ModelMemoryLifecycleProjection]:
        """Get EXPIRED memories that are ready for archival.

        Query: lifecycle_state = 'expired' AND archived_at IS NULL
               AND archive_initiated_at IS NULL

        Args:
            now: Current time (for consistent evaluation).
            domain: Domain scope for the query.
            correlation_id: Correlation ID for tracing.
            limit: Maximum number of results to return.

        Returns:
            List of projections for memories eligible for archival.
        """
        ...


class ModelMemoryLifecycleProjection(  # omnimemory-model-exempt: projection model
    BaseModel
):
    """Projection of memory lifecycle state for tick evaluation.

    This is a read-optimized view of memory state specifically for
    lifecycle orchestration decisions. It contains only the fields
    needed for expiration and archival evaluation.

    Note:
        This is a placeholder model. The actual projection will be
        managed by the memory lifecycle reducer (OMN-1453 P4c).

    Attributes:
        entity_id: The memory entity UUID.
        lifecycle_state: Current state (active, stale, expired, archived, deleted).
        expires_at: TTL deadline (None = no expiration).
        expired_at: When memory transitioned to EXPIRED.
        archived_at: When memory was archived (None = not archived).
        lifecycle_revision: Revision for optimistic locking.
        expiration_emitted_at: When expiration event was emitted (dedup marker).
        archive_initiated_at: When archive initiation was emitted (dedup marker).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    entity_id: UUID = Field(..., description="The memory entity UUID.")
    lifecycle_state: str = Field(..., description="Current lifecycle state.")
    expires_at: datetime | None = Field(
        default=None, description="TTL deadline (None = no expiration)."
    )
    expired_at: datetime | None = Field(
        default=None, description="When memory transitioned to EXPIRED."
    )
    archived_at: datetime | None = Field(
        default=None, description="When memory was archived."
    )
    lifecycle_revision: int = Field(
        default=1, ge=1, description="Revision for optimistic locking."
    )
    # Deduplication markers
    expiration_emitted_at: datetime | None = Field(
        default=None, description="When expiration event was emitted."
    )
    archive_initiated_at: datetime | None = Field(
        default=None, description="When archive initiation was emitted."
    )

    def needs_expiration_event(self, now: datetime) -> bool:
        """Check if this memory needs an expiration event emitted.

        Returns True if:
            - lifecycle_state == ACTIVE (using EnumLifecycleState)
            - expires_at is not None AND expires_at <= now
            - expiration_emitted_at is None (not already emitted)

        Args:
            now: Current time for deadline comparison.

        Returns:
            True if expiration event should be emitted.
        """
        return (
            self.lifecycle_state == EnumLifecycleState.ACTIVE.value
            and self.expires_at is not None
            and self.expires_at <= now
            and self.expiration_emitted_at is None
        )

    def needs_archive_event(self, now: datetime) -> bool:
        """Check if this memory needs an archive initiation event.

        Returns True if:
            - lifecycle_state == EXPIRED (using EnumLifecycleState)
            - archived_at is None (not yet archived)
            - archive_initiated_at is None (not already initiated)

        Args:
            now: Current time (unused, for consistent interface).

        Returns:
            True if archive initiation event should be emitted.
        """
        return (
            self.lifecycle_state == EnumLifecycleState.EXPIRED.value
            and self.archived_at is None
            and self.archive_initiated_at is None
        )


# =============================================================================
# HANDLER
# =============================================================================


class HandlerMemoryTick:
    """Handler for RuntimeTick - memory lifecycle TTL evaluation.

    This handler processes runtime tick events and scans the memory
    lifecycle projection for entities with expired TTLs or pending
    archival. It emits lifecycle transition events for entities that
    need them, using projection emission markers for deduplication.

    Container-Driven Pattern:
        This handler follows the ONEX container-driven initialization pattern:
        - Constructor takes only ModelONEXContainer for DI compliance
        - Dependencies are injected via async initialize() method
        - health_check() provides health status for monitoring
        - describe() provides handler metadata for introspection

    Lifecycle Evaluation:
        The handler performs two scans on each tick:
        1. Expiration: Find ACTIVE memories with expires_at <= now
        2. Archive initiation: Find EXPIRED memories pending archive

    Projection Queries:
        Uses dedicated projection reader methods that filter by:
        - Deadline < now (TTL has passed)
        - Emission marker IS NULL (not yet emitted)
        - Appropriate lifecycle state (ACTIVE for expiration, EXPIRED for archive)

    Time Injection:
        Uses the `now` timestamp from RuntimeTick for deterministic
        evaluation, enabling reproducible tests and consistent behavior
        across distributed deployments.

    Attributes:
        _container: ONEX container for dependency injection.
        _projection_reader: Reader for memory lifecycle projection state.
        _batch_size: Maximum memories to process per tick (default: 100).
        _initialized: Whether initialize() has been called.

    Example:
        >>> from datetime import datetime, timezone
        >>> from uuid import uuid4
        >>> from omnibase_core.container import ModelONEXContainer
        >>> container = ModelONEXContainer()
        >>> handler = HandlerMemoryTick(container)
        >>> await handler.initialize(projection_reader=reader, batch_size=100)
        >>> tick_time = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        >>> runtime_tick = ModelRuntimeTick(
        ...     now=tick_time,
        ...     tick_id=uuid4(),
        ...     sequence_number=1,
        ...     scheduled_at=tick_time,
        ...     correlation_id=uuid4(),
        ...     scheduler_id="runtime-001",
        ...     tick_interval_ms=1000,
        ... )
        >>> output = await handler.handle(envelope)
        >>> # Output events: ModelMemoryExpiredEvent, ModelMemoryArchiveInitiated
    """

    def __init__(self, container: ModelONEXContainer) -> None:
        """Initialize HandlerMemoryTick with ONEX container for dependency injection.

        Args:
            container: ONEX container providing dependency injection for
                services, configuration, and runtime context.

        Note:
            The container is stored for interface compliance with the standard ONEX
            handler pattern (def __init__(self, container: ModelONEXContainer)) and
            to enable future DI-based service resolution. Call initialize() to
            configure the handler with its dependencies before use.
        """
        self._container = container
        self._projection_reader: ProtocolMemoryLifecycleProjectionReader | None = None
        self._batch_size: int = 100
        self._circuit_breaker: CircuitBreaker | None = None
        self._initialized: bool = False

    async def initialize(
        self,
        projection_reader: ProtocolMemoryLifecycleProjectionReader | None = None,
        batch_size: int = 100,
        circuit_breaker: CircuitBreaker | None = None,
    ) -> None:
        """Initialize the handler with its dependencies.

        This method must be called after construction to configure the handler
        with a projection reader and optional settings.

        Args:
            projection_reader: Reader for querying memory lifecycle projection state.
                If None, handler will return empty results (useful for testing).
            batch_size: Maximum number of memories to process per tick.
                Prevents tick processing from blocking too long.
            circuit_breaker: Optional circuit breaker for projection reader resilience.
                If None, a default circuit breaker is created with conservative settings.
        """
        self._projection_reader = projection_reader
        self._batch_size = batch_size
        # Circuit breaker for projection reader calls - protects against
        # cascading failures when the projection reader is unavailable
        self._circuit_breaker = circuit_breaker or CircuitBreaker(
            failure_threshold=5,
            recovery_timeout=30.0,
            success_threshold=2,
        )
        self._initialized = True

        logger.info(
            "HandlerMemoryTick initialized",
            extra={
                "handler_id": self.handler_id,
                "batch_size": self._batch_size,
                "has_projection_reader": self._projection_reader is not None,
            },
        )

    @property
    def initialized(self) -> bool:
        """Return whether the handler has been initialized.

        Returns:
            True if initialize() has been called, False otherwise.
        """
        return self._initialized

    async def shutdown(self) -> None:
        """Shutdown the handler and release resources.

        Resets initialization state and clears internal references.
        Safe to call multiple times (idempotent).
        After shutdown, initialize() must be called again to use the handler.

        Note:
            This method does NOT close the projection reader as it is an
            external resource whose lifecycle is not owned by this handler.
        """
        if self._initialized:
            # Clear internal state (external resources are not closed)
            self._projection_reader = None
            self._circuit_breaker = None
            self._initialized = False
            logger.info("HandlerMemoryTick shutdown complete")

    async def health_check(self) -> ModelMemoryTickHealth:
        """Check the health status of the handler.

        Returns:
            ModelMemoryTickHealth with detailed status information:
            - initialized: Whether the handler has been initialized
            - circuit_breaker_state: Current state of the circuit breaker
            - projection_reader_available: Whether a projection reader is configured
            - batch_size: Configured batch size for tick processing
        """
        circuit_state: str | None = None
        if self._circuit_breaker is not None:
            circuit_state = self._circuit_breaker.state.value

        return ModelMemoryTickHealth(
            initialized=self._initialized,
            circuit_breaker_state=circuit_state,
            projection_reader_available=self._projection_reader is not None,
            batch_size=self._batch_size,
        )

    async def describe(self) -> ModelMemoryTickMetadata:
        """Return metadata and capabilities of this handler.

        Provides introspection information about the handler, including
        its purpose, supported operations, and configuration.

        Returns:
            ModelMemoryTickMetadata with handler information including
            name, description, capabilities, and message types.
        """
        return ModelMemoryTickMetadata(
            name="HandlerMemoryTick",
            description=(
                "Handler for RuntimeTick - evaluates memory entities for TTL "
                "expiration and archive eligibility. Emits lifecycle transition "
                "events for expired and archive-ready memories."
            ),
            capabilities=[
                "memory_expiration",
                "archive_initiation",
                "batch_processing",
                "circuit_breaker_protection",
            ],
            initialized=self._initialized,
            message_types=list(self.message_types),
            node_kind=self.node_kind.value,
        )

    @property
    def handler_id(self) -> str:
        """Return unique identifier for this handler."""
        return "handler-memory-tick"

    @property
    def category(self) -> EnumMessageCategory:
        """Return the message category this handler processes."""
        return EnumMessageCategory.COMMAND

    @property
    def message_types(self) -> set[str]:
        """Return the set of message types this handler processes."""
        return {"ModelRuntimeTick"}

    @property
    def node_kind(self) -> EnumNodeKind:
        """Return the node kind this handler belongs to."""
        return EnumNodeKind.ORCHESTRATOR

    async def handle(
        self,
        envelope: ModelEventEnvelope[ModelRuntimeTick],
    ) -> ModelHandlerOutput[None]:
        """Process runtime tick and emit lifecycle transition events.

        Scans the memory lifecycle projection for:
        1. ACTIVE memories with expired TTL -> emit ModelMemoryExpiredEvent
        2. EXPIRED memories pending archive -> emit ModelMemoryArchiveInitiated

        Uses projection emission markers to prevent duplicate events.

        Args:
            envelope: The event envelope containing the runtime tick event.

        Returns:
            ModelHandlerOutput containing lifecycle events (ModelMemoryExpiredEvent,
            ModelMemoryArchiveInitiated). Events tuple may be empty if no
            transitions detected.

        Note:
            The actual projection queries are placeholders (return empty lists)
            until OMN-1524 (Infra Projection Reader Primitives) is implemented.
        """
        start_time = time.perf_counter()

        # Extract from envelope
        tick = envelope.payload
        # Use tick.now (injected time) for consistent evaluation across distributed
        # deployments and deterministic testing - NOT envelope_timestamp
        now = tick.now
        correlation_id = envelope.correlation_id or uuid4()

        events: list[BaseModel] = []

        # 1. Check for expired memories (ACTIVE -> EXPIRED)
        expired_events = await self._check_memory_expirations(
            tick=tick,
            now=now,
            correlation_id=correlation_id,
        )
        events.extend(expired_events)

        # 2. Check for archive candidates (EXPIRED -> ARCHIVED)
        archive_events = await self._check_archive_candidates(
            tick=tick,
            now=now,
            correlation_id=correlation_id,
        )
        events.extend(archive_events)

        if events:
            logger.info(
                "MemoryTick processed, emitting lifecycle events",
                extra={
                    "tick_id": str(tick.tick_id),
                    "sequence_number": tick.sequence_number,
                    "expired_count": len(expired_events),
                    "archive_initiated_count": len(archive_events),
                    "correlation_id": str(correlation_id),
                },
            )

        # Build result for metrics/observability
        result = ModelMemoryTickResult(
            expired_count=len(expired_events),
            archive_initiated_count=len(archive_events),
            tick_id=tick.tick_id,
            sequence_number=tick.sequence_number,
            evaluated_at=now,
        )

        # Calculate processing time
        processing_time_ms = (time.perf_counter() - start_time) * 1000

        # Return handler output (ORCHESTRATOR: events only, no result)
        return ModelHandlerOutput.for_orchestrator(
            input_envelope_id=envelope.envelope_id,
            correlation_id=correlation_id,
            handler_id=self.handler_id,
            events=tuple(events),
            metrics={
                "expired_count": float(result.expired_count),
                "archive_initiated_count": float(result.archive_initiated_count),
                "batch_size": float(self._batch_size),
            },
            processing_time_ms=processing_time_ms,
        )

    async def _check_memory_expirations(
        self,
        tick: ModelRuntimeTick,
        now: datetime,
        correlation_id: UUID,
    ) -> list[ModelMemoryExpiredEvent]:
        """Check for ACTIVE memories with expired TTL.

        Queries the projection for memories that:
        - Have lifecycle_state = 'active'
        - Have expires_at IS NOT NULL AND expires_at <= now
        - Have NOT already had expiration event emitted

        Args:
            tick: The runtime tick event (used for causation_id).
            now: Current time for deadline comparison.
            correlation_id: Correlation ID for tracing.

        Returns:
            List of ModelMemoryExpiredEvent events to emit.
        """
        if self._projection_reader is None:
            # No projection reader configured - return empty
            # This is expected during initial development and testing
            logger.debug(
                "No projection reader configured, skipping expiration check",
                extra={"correlation_id": str(correlation_id)},
            )
            return []

        # Type narrowing for circuit breaker (guaranteed set after initialize())
        assert self._circuit_breaker is not None

        # Check circuit breaker before attempting external call
        if not self._circuit_breaker.should_allow_request():
            logger.warning(
                "Circuit breaker OPEN for projection reader, skipping expiration check",
                extra={
                    "correlation_id": str(correlation_id),
                    "circuit_breaker_state": self._circuit_breaker.state.value,
                },
            )
            return []

        # Query projection reader primitives
        # Query projection for expired candidates with timeout and circuit breaker
        try:
            expired_projections = await asyncio.wait_for(
                self._projection_reader.get_expired_candidates(
                    now=now,
                    domain="memory",
                    correlation_id=correlation_id,
                    limit=self._batch_size,
                ),
                timeout=_PROJECTION_READER_TIMEOUT_SECONDS,
            )
            self._circuit_breaker.record_success()
        except TimeoutError:
            self._circuit_breaker.record_timeout()
            logger.error(
                "Projection reader timeout during expiration check",
                extra={
                    "correlation_id": str(correlation_id),
                    "timeout_seconds": _PROJECTION_READER_TIMEOUT_SECONDS,
                },
            )
            return []
        except CircuitBreakerOpenError:
            logger.warning(
                "Circuit breaker rejected expiration check request",
                extra={"correlation_id": str(correlation_id)},
            )
            return []
        except Exception as exc:
            self._circuit_breaker.record_failure()
            logger.error(
                "Projection reader error during expiration check",
                extra={
                    "correlation_id": str(correlation_id),
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                },
            )
            return []

        events: list[ModelMemoryExpiredEvent] = []

        for projection in expired_projections:
            # Double-check with projection helper (defensive)
            if not projection.needs_expiration_event(now):
                continue

            # Type narrowing: needs_expiration_event() guarantees expires_at is not None
            expires_at = projection.expires_at
            if expires_at is None:
                # This should never happen - needs_expiration_event() ensures expires_at
                # is not None. Log and skip as defensive measure.
                logger.warning(
                    "Skipping projection with None expires_at despite passing "
                    "needs_expiration_event() check - this indicates a bug",
                    extra={
                        "entity_id": str(projection.entity_id),
                        "correlation_id": str(correlation_id),
                    },
                )
                continue

            event = ModelMemoryExpiredEvent(
                entity_id=projection.entity_id,
                memory_id=projection.entity_id,
                correlation_id=correlation_id,
                causation_id=tick.tick_id,
                emitted_at=now,
                expires_at=expires_at,
                lifecycle_revision=projection.lifecycle_revision,
            )
            events.append(event)

            logger.info(
                "Detected memory expiration",
                extra={
                    "memory_id": str(projection.entity_id),
                    "expires_at": expires_at.isoformat(),
                    "lifecycle_revision": projection.lifecycle_revision,
                    "correlation_id": str(correlation_id),
                },
            )

        return events

    async def _check_archive_candidates(
        self,
        tick: ModelRuntimeTick,
        now: datetime,
        correlation_id: UUID,
    ) -> list[ModelMemoryArchiveInitiated]:
        """Check for EXPIRED memories ready for archival.

        Queries the projection for memories that:
        - Have lifecycle_state = 'expired'
        - Have archived_at IS NULL (not yet archived)
        - Have NOT already had archive initiation emitted

        Args:
            tick: The runtime tick event (used for causation_id).
            now: Current time for consistent evaluation.
            correlation_id: Correlation ID for tracing.

        Returns:
            List of ModelMemoryArchiveInitiated events to emit.
        """
        if self._projection_reader is None:
            # No projection reader configured - return empty
            logger.debug(
                "No projection reader configured, skipping archive check",
                extra={"correlation_id": str(correlation_id)},
            )
            return []

        # Type narrowing for circuit breaker (guaranteed set after initialize())
        assert self._circuit_breaker is not None

        # Check circuit breaker before attempting external call
        if not self._circuit_breaker.should_allow_request():
            logger.warning(
                "Circuit breaker OPEN for projection reader, skipping archive check",
                extra={
                    "correlation_id": str(correlation_id),
                    "circuit_breaker_state": self._circuit_breaker.state.value,
                },
            )
            return []

        # Query projection reader primitives
        # Query projection for archive candidates with timeout and circuit breaker
        try:
            archive_projections = await asyncio.wait_for(
                self._projection_reader.get_archive_candidates(
                    now=now,
                    domain="memory",
                    correlation_id=correlation_id,
                    limit=self._batch_size,
                ),
                timeout=_PROJECTION_READER_TIMEOUT_SECONDS,
            )
            self._circuit_breaker.record_success()
        except TimeoutError:
            self._circuit_breaker.record_timeout()
            logger.error(
                "Projection reader timeout during archive check",
                extra={
                    "correlation_id": str(correlation_id),
                    "timeout_seconds": _PROJECTION_READER_TIMEOUT_SECONDS,
                },
            )
            return []
        except CircuitBreakerOpenError:
            logger.warning(
                "Circuit breaker rejected archive check request",
                extra={"correlation_id": str(correlation_id)},
            )
            return []
        except Exception as exc:
            self._circuit_breaker.record_failure()
            logger.error(
                "Projection reader error during archive check",
                extra={
                    "correlation_id": str(correlation_id),
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                },
            )
            return []

        events: list[ModelMemoryArchiveInitiated] = []

        for projection in archive_projections:
            # Double-check with projection helper (defensive)
            if not projection.needs_archive_event(now):
                continue

            event = ModelMemoryArchiveInitiated(
                entity_id=projection.entity_id,
                memory_id=projection.entity_id,
                correlation_id=correlation_id,
                causation_id=tick.tick_id,
                emitted_at=now,
                expired_at=projection.expired_at,
                lifecycle_revision=projection.lifecycle_revision,
            )
            events.append(event)

            logger.info(
                "Initiating memory archival",
                extra={
                    "memory_id": str(projection.entity_id),
                    "expired_at": (
                        projection.expired_at.isoformat()
                        if projection.expired_at
                        else None
                    ),
                    "lifecycle_revision": projection.lifecycle_revision,
                    "correlation_id": str(correlation_id),
                },
            )

        return events


__all__: list[str] = [
    "HandlerMemoryTick",
    "ModelMemoryArchiveInitiated",
    "ModelMemoryExpiredEvent",
    "ModelMemoryLifecycleProjection",
    "ModelMemoryTickHealth",
    "ModelMemoryTickMetadata",
    "ModelMemoryTickResult",
    "ProtocolMemoryLifecycleProjectionReader",
]
