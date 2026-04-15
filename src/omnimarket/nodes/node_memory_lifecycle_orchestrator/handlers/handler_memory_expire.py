# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2025 OmniNode Team
"""Handler for memory expiration with optimistic locking.

orchestrator. It performs state transitions from ACTIVE to EXPIRED using
optimistic concurrency control to handle concurrent access safely.

Optimistic Locking Pattern:
    The handler uses a revision-based optimistic locking strategy where each
    memory entity has a `lifecycle_revision` counter. State transitions only
    succeed when the expected revision matches the current database revision.
    On conflict (another process updated the entity), the caller can retry
    with the updated revision.

    This pattern is preferred over pessimistic locking (SELECT FOR UPDATE)
    because:
    - It allows higher read concurrency
    - It avoids deadlocks in distributed systems
    - It scales better with multiple orchestrator instances

SQL Pattern:
    UPDATE memories
    SET lifecycle_state = 'expired',
        expired_at = :now,
        lifecycle_revision = lifecycle_revision + 1,
        updated_at = :now
    WHERE id = :memory_id
      AND lifecycle_revision = :expected_revision
      AND lifecycle_state = 'active'

    If rows_affected == 0 -> conflict (another process updated first)

Related:
    - OMN-1453: OmniMemory P4b - Lifecycle Orchestrator Database Integration
    - OMN-1524: Infra transaction helper primitives (pending)

Example::

    from omnibase_core.container import ModelONEXContainer
    from omnimarket.nodes.node_memory_lifecycle_orchestrator.handlers import (
        HandlerMemoryExpire,
        ModelExpireMemoryCommand,
    )

    # Create handler with container (dependency injection)
    container = ModelONEXContainer()
    handler = HandlerMemoryExpire(container)

    # Initialize with database pool and configuration
    await handler.initialize(db_pool=pool, max_retries=3)

    # Execute expiration
    result = await handler.handle(
        ModelExpireMemoryCommand(
            memory_id=uuid4(),
            expected_revision=5,
            reason="ttl_expired",
        )
    )

    if result.success:
        print(f"Memory expired, new revision: {result.new_revision}")
    elif result.conflict:
        print("Conflict detected, retry with updated revision")
    else:
        print(f"Error: {result.error_message}")

.. versionadded:: 0.1.0
    Initial implementation for OMN-1453.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from asyncpg import Pool
    from asyncpg.exceptions import InterfaceError, InternalClientError, PostgresError
else:
    try:
        from asyncpg.exceptions import (
            InterfaceError,
            InternalClientError,
            PostgresError,
        )
    except ImportError:
        # Fallback if asyncpg not installed - use base Exception
        # This allows the module to be imported even without asyncpg
        PostgresError = Exception  # type: ignore[misc,assignment]
        InterfaceError = Exception  # type: ignore[misc,assignment]
        InternalClientError = Exception  # type: ignore[misc,assignment]

from typing import TYPE_CHECKING

from omnimemory.enums import EnumLifecycleState
from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from omnibase_core.container import ModelONEXContainer
from omnimemory.utils.concurrency import (
    CircuitBreaker,
    CircuitBreakerOpenError,
    CircuitBreakerState,
)

logger = logging.getLogger(__name__)

# Query timeout for database operations (seconds)
_QUERY_TIMEOUT_SECONDS: float = 30.0

# Circuit breaker configuration for database operations
_CIRCUIT_BREAKER_FAILURE_THRESHOLD: int = 5
_CIRCUIT_BREAKER_RECOVERY_TIMEOUT: float = 60.0
_CIRCUIT_BREAKER_SUCCESS_THRESHOLD: int = 2

__all__ = [
    "CircuitBreakerOpenError",
    "HandlerMemoryExpire",
    "ModelExpireMemoryCommand",
    "ModelMemoryCurrentState",
    "ModelMemoryExpireHealth",
    "ModelMemoryExpireMetadata",
    "ModelMemoryExpireResult",
]


# =============================================================================
# Models
# =============================================================================


class ModelExpireMemoryCommand(BaseModel):  # omnimemory-model-exempt: handler command
    """Command to expire a memory entity.

    This command initiates a state transition from ACTIVE to EXPIRED using
    optimistic concurrency control. The expected_revision must match the
    current revision in the database for the transition to succeed.

    Attributes:
        memory_id: UUID of the memory entity to expire.
        expected_revision: Expected lifecycle revision for optimistic lock.
            Must match current revision in database.
        reason: Reason for expiration (for audit trail).
        expired_at: Timestamp for expiration (defaults to current time).
            Allows injecting test timestamps for deterministic testing.
    """

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=True,
    )

    memory_id: UUID = Field(
        ...,
        description="UUID of the memory entity to expire",
    )
    expected_revision: int = Field(
        ...,
        ge=0,
        description="Expected lifecycle revision for optimistic lock",
    )
    reason: str = Field(
        default="ttl_expired",
        min_length=1,
        max_length=256,
        description="Reason for expiration (for audit trail)",
    )
    expired_at: datetime | None = Field(
        default=None,
        description="Optional explicit expiration timestamp (defaults to now)",
    )


class ModelMemoryExpireResult(BaseModel):  # omnimemory-model-exempt: handler result
    """Result of memory expiration attempt.

    Contains the outcome of the expiration operation, including success/failure
    status, conflict detection, and any error details.

    State Interpretations:
        - success=True, conflict=False: Expiration succeeded
        - success=False, conflict=True: Concurrent modification detected, retry eligible
        - success=False, conflict=False: Hard failure (invalid state, not found, etc.)

    Attributes:
        memory_id: UUID of the memory entity.
        success: Whether the expiration succeeded.
        new_revision: New revision after successful transition (None on failure).
        conflict: Whether a concurrent modification was detected.
        error_message: Detailed error message on failure.
        previous_state: The state before transition (if known).
    """

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=True,
    )

    memory_id: UUID = Field(
        ...,
        description="UUID of the memory entity",
    )
    success: bool = Field(
        ...,
        description="Whether the expiration succeeded",
    )
    new_revision: int | None = Field(
        default=None,
        description="New revision after successful transition",
    )
    conflict: bool = Field(
        default=False,
        description="Whether a concurrent modification was detected",
    )
    error_message: str | None = Field(
        default=None,
        description="Detailed error message on failure",
    )
    previous_state: EnumLifecycleState | None = Field(
        default=None,
        description="The state before transition attempt (if known)",
    )


class ModelMemoryCurrentState(BaseModel):  # omnimemory-model-exempt: handler state
    """Current state of a memory entity for retry operations.

    Used by handle_with_retry() to re-read current state after conflict.

    Attributes:
        memory_id: UUID of the memory entity.
        lifecycle_state: Current lifecycle state.
        lifecycle_revision: Current revision number.
        updated_at: Timestamp of last update.
    """

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=True,
    )

    memory_id: UUID = Field(
        ...,
        description="UUID of the memory entity",
    )
    lifecycle_state: EnumLifecycleState = Field(
        ...,
        description="Current lifecycle state",
    )
    lifecycle_revision: int = Field(
        ...,
        ge=0,
        description="Current revision number",
    )
    updated_at: datetime = Field(
        ...,
        description="Timestamp of last update",
    )


class ModelMemoryExpireHealth(BaseModel):  # omnimemory-model-exempt: handler health
    """Health status for the Memory Expire Handler.

    Returned by health_check() to provide detailed health information
    about the handler and its database connectivity.

    Attributes:
        initialized: Whether the handler has been initialized.
        circuit_breaker_state: Current state of the circuit breaker.
        db_pool_available: Whether the database connection pool is configured.
        max_retries: Configured maximum retry attempts.
    """

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
    )

    initialized: bool = Field(
        ...,
        description="Whether the handler has been initialized",
    )
    circuit_breaker_state: str = Field(
        ...,
        description="Current state of the circuit breaker (closed, open, half_open, not_configured)",
    )
    db_pool_available: bool = Field(
        ...,
        description="Whether the database connection pool is configured",
    )
    max_retries: int = Field(
        ...,
        ge=1,
        description="Configured maximum retry attempts",
    )


class ModelMemoryExpireMetadata(BaseModel):  # omnimemory-model-exempt: handler metadata
    """Metadata describing memory expire handler capabilities and configuration.

    Returned by describe() method to provide introspection information
    about the handler's purpose, capabilities, and state transition rules.

    Attributes:
        name: Handler class name.
        description: Brief description of handler purpose.
        version: Handler version string.
        capabilities: List of supported capabilities.
        valid_from_states: States from which expiration can transition.
        target_state: The state memories transition to after expiration.
        initialized: Whether the handler has been initialized.
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
    version: str = Field(
        ...,
        description="Handler version string",
    )
    capabilities: list[str] = Field(
        ...,
        description="List of supported capabilities",
    )
    valid_from_states: list[str] = Field(
        ...,
        description="States from which expiration can transition",
    )
    target_state: str = Field(
        ...,
        description="The state memories transition to after expiration",
    )
    initialized: bool = Field(
        ...,
        description="Whether the handler has been initialized",
    )


# =============================================================================
# Handler
# =============================================================================


class HandlerMemoryExpire:
    """Handler for expiring memories with optimistic locking.

    Performs state transition: ACTIVE -> EXPIRED

    Uses optimistic concurrency control to safely handle concurrent access:
    1. Check current revision matches expected revision
    2. If match: update state, increment revision, commit
    3. If mismatch: return conflict result (caller can retry with updated revision)

    The handler is designed to be stateless and idempotent - the same command
    with the same expected_revision will always produce the same result (either
    success or conflict, never partial state).

    Attributes:
        max_retries: Maximum retry attempts for handle_with_retry().
    """

    # SQL for atomic state transition with optimistic locking
    # The WHERE clause ensures both revision AND state match expected values
    _EXPIRE_SQL = """
        UPDATE memories
        SET lifecycle_state = $1,
            expired_at = $2,
            lifecycle_revision = lifecycle_revision + 1,
            updated_at = $3
        WHERE id = $4
          AND lifecycle_revision = $5
          AND lifecycle_state = $6
        RETURNING lifecycle_revision
    """

    # SQL to read current state for retry operations
    _READ_STATE_SQL = """
        SELECT id, lifecycle_state, lifecycle_revision, updated_at
        FROM memories
        WHERE id = $1
    """

    # Valid source states for expiration transition.
    # Only ACTIVE memories can be expired - the SQL WHERE clause requires
    # lifecycle_state = 'active'. Including EXPIRED here would cause
    # handle_with_retry() to keep retrying on already-expired memories.
    _VALID_FROM_STATES = frozenset({EnumLifecycleState.ACTIVE})

    def __init__(self, container: ModelONEXContainer) -> None:
        """Initialize the expiration handler with container.

        Args:
            container: ONEX dependency injection container for service resolution.
                The handler uses the container for accessing shared infrastructure
                services and configuration.

        Note:
            The handler is not fully operational until initialize() is called.
            Use the initialized property to check initialization status.
        """
        self._container = container
        self._db_pool: Pool | None = None
        self._max_retries: int = 3
        self._circuit_breaker: CircuitBreaker | None = None
        self._initialized: bool = False

    async def initialize(
        self,
        db_pool: Pool,
        max_retries: int = 3,
        circuit_breaker: CircuitBreaker | None = None,
    ) -> None:
        """Initialize handler resources.

        Must be called before using handle() or handle_with_retry().

        Args:
            db_pool: Database connection pool for database operations.
                Required for all database operations.
            max_retries: Maximum retry attempts for handle_with_retry().
                Defaults to 3, which balances retry opportunity against
                excessive contention scenarios.
            circuit_breaker: Optional circuit breaker for database operations.
                If None, a default circuit breaker is created with standard
                settings (5 failures to open, 60s recovery, 2 successes to close).
                Protects against cascading failures during database outages.

        Raises:
            ValueError: If max_retries is less than 1.
        """
        if max_retries < 1:
            raise ValueError(f"max_retries must be >= 1, got {max_retries}")

        self._db_pool = db_pool
        self._max_retries = max_retries
        self._circuit_breaker = circuit_breaker or CircuitBreaker(
            failure_threshold=_CIRCUIT_BREAKER_FAILURE_THRESHOLD,
            recovery_timeout=_CIRCUIT_BREAKER_RECOVERY_TIMEOUT,
            success_threshold=_CIRCUIT_BREAKER_SUCCESS_THRESHOLD,
        )
        self._initialized = True
        logger.info("HandlerMemoryExpire initialized successfully")

    @property
    def initialized(self) -> bool:
        """Check if the handler has been initialized.

        Returns:
            True if initialize() has been called successfully.
        """
        return self._initialized

    async def shutdown(self) -> None:
        """Shutdown the handler and release resources.

        Resets initialization state and clears internal references.
        Safe to call multiple times (idempotent).
        After shutdown, initialize() must be called again to use the handler.

        Note:
            This method does NOT close the database pool as it is an
            external resource whose lifecycle is not owned by this handler.
        """
        if self._initialized:
            # Clear internal state (pools are external, don't close)
            self._db_pool = None
            self._circuit_breaker = None
            self._initialized = False
            logger.info("HandlerMemoryExpire shutdown complete")

    async def health_check(self) -> ModelMemoryExpireHealth:
        """Check handler health status.

        Returns:
            ModelMemoryExpireHealth with detailed status information:
            - initialized: Whether initialize() has been called
            - circuit_breaker_state: Current circuit breaker state
            - db_pool_available: Whether database pool is configured
            - max_retries: Configured retry limit
        """
        cb_state = (
            self._circuit_breaker.state.value
            if self._circuit_breaker
            else "not_configured"
        )

        return ModelMemoryExpireHealth(
            initialized=self._initialized,
            circuit_breaker_state=cb_state,
            db_pool_available=self._db_pool is not None,
            max_retries=self._max_retries,
        )

    async def describe(self) -> ModelMemoryExpireMetadata:
        """Return handler metadata and capabilities.

        Provides information about the handler for introspection
        and service discovery.

        Returns:
            ModelMemoryExpireMetadata with handler information including
            name, description, capabilities, and state transition rules.
        """
        return ModelMemoryExpireMetadata(
            name="HandlerMemoryExpire",
            description=(
                "Handles memory expiration with optimistic locking. "
                "Performs ACTIVE -> EXPIRED state transitions."
            ),
            version="1.0.0",
            capabilities=[
                "memory_expiration",
                "optimistic_locking",
                "retry_with_backoff",
            ],
            valid_from_states=[s.value for s in self._VALID_FROM_STATES],
            target_state=EnumLifecycleState.EXPIRED.value,
            initialized=self._initialized,
        )

    @property
    def max_retries(self) -> int:
        """Maximum retry attempts for handle_with_retry()."""
        return self._max_retries

    @property
    def circuit_breaker(self) -> CircuitBreaker:
        """Circuit breaker protecting database operations.

        Exposed for monitoring and testing purposes. The circuit breaker
        tracks database failures and opens to prevent cascading failures.
        """
        assert self._circuit_breaker is not None
        return self._circuit_breaker

    @property
    def circuit_breaker_state(self) -> CircuitBreakerState:
        """Current state of the circuit breaker.

        Returns:
            CircuitBreakerState.CLOSED: Normal operation
            CircuitBreakerState.OPEN: Failing fast, DB assumed unavailable
            CircuitBreakerState.HALF_OPEN: Testing if DB has recovered
        """
        assert self._circuit_breaker is not None
        return self._circuit_breaker.state

    async def handle(
        self,
        command: ModelExpireMemoryCommand,
    ) -> ModelMemoryExpireResult:
        """Handle expire command with optimistic locking.

        Performs the ACTIVE -> EXPIRED state transition atomically using
        optimistic concurrency control. The transition only succeeds if:
        - The memory entity exists
        - The current revision matches expected_revision
        - The current state is ACTIVE (or other valid source state)

        Args:
            command: Expiration command with memory ID and expected revision.

        Returns:
            ModelMemoryExpireResult indicating:
            - success=True: Transition completed, new_revision populated
            - success=False, conflict=True: Revision mismatch, retry eligible
            - success=False, conflict=False: Invalid state or not found
        """
        # Log the operation for observability
        logger.info(
            "Expiring memory %s (expected_revision=%d, reason=%s)",
            command.memory_id,
            command.expected_revision,
            command.reason,
        )

        # Require handler to be initialized
        if not self._initialized:
            raise RuntimeError(
                "Handler not initialized. Call initialize() before handle()."
            )

        # Require database pool for all operations (should always be true after init)
        if self._db_pool is None:
            raise RuntimeError(
                "Database pool not configured. "
                "Initialize handler with db_pool parameter."
            )

        # Type narrowing for circuit breaker (guaranteed set after initialize())
        assert self._circuit_breaker is not None

        # Check circuit breaker before attempting database operation
        if not self._circuit_breaker.should_allow_request():
            logger.warning(
                "Circuit breaker open, failing fast for memory %s",
                command.memory_id,
            )
            return ModelMemoryExpireResult(
                memory_id=command.memory_id,
                success=False,
                conflict=False,
                error_message=(
                    "Database circuit breaker is open. "
                    "Service is temporarily unavailable."
                ),
            )

        # Determine expiration timestamp
        expired_at = command.expired_at or datetime.now(UTC)

        try:
            async with self._db_pool.acquire() as conn:
                row = await asyncio.wait_for(
                    conn.fetchrow(
                        self._EXPIRE_SQL,
                        EnumLifecycleState.EXPIRED.value,  # $1: new state
                        expired_at,  # $2: expired_at
                        expired_at,  # $3: updated_at
                        command.memory_id,  # $4: id
                        command.expected_revision,  # $5: expected revision
                        EnumLifecycleState.ACTIVE.value,  # $6: required current state
                    ),
                    timeout=_QUERY_TIMEOUT_SECONDS,
                )

                if row is None:
                    # No rows updated - either revision mismatch or invalid state
                    # Try to read current state for better error message
                    try:
                        current_state = await self._read_current_state(
                            command.memory_id
                        )
                    except CircuitBreakerOpenError:
                        # Circuit breaker opened during state read
                        return ModelMemoryExpireResult(
                            memory_id=command.memory_id,
                            success=False,
                            conflict=False,
                            error_message=(
                                "Database circuit breaker is open. "
                                "Unable to read current state for diagnostics."
                            ),
                        )
                    except (
                        PostgresError,
                        InterfaceError,
                        InternalClientError,
                        TimeoutError,
                    ) as e:
                        # Database error during state read - log and return generic error
                        logger.warning(
                            "Error reading current state for memory %s: %s",
                            command.memory_id,
                            e,
                        )
                        return ModelMemoryExpireResult(
                            memory_id=command.memory_id,
                            success=False,
                            conflict=False,
                            error_message=(
                                "Expiration failed and unable to determine cause. "
                                f"State read error: {e}"
                            ),
                        )

                    if current_state is None:
                        self._circuit_breaker.record_success()
                        return ModelMemoryExpireResult(
                            memory_id=command.memory_id,
                            success=False,
                            conflict=False,
                            error_message="Memory not found",
                        )

                    if current_state.lifecycle_revision != command.expected_revision:
                        self._circuit_breaker.record_success()
                        return ModelMemoryExpireResult(
                            memory_id=command.memory_id,
                            success=False,
                            conflict=True,
                            previous_state=current_state.lifecycle_state,
                            error_message=(
                                f"Revision conflict: expected {command.expected_revision}, "
                                f"found {current_state.lifecycle_revision}"
                            ),
                        )

                    # Revision matched but state was wrong
                    self._circuit_breaker.record_success()
                    return ModelMemoryExpireResult(
                        memory_id=command.memory_id,
                        success=False,
                        conflict=False,
                        previous_state=current_state.lifecycle_state,
                        error_message=(
                            f"Cannot expire memory in state {current_state.lifecycle_state.value}. "
                            "Only ACTIVE memories can be expired."
                        ),
                    )

                # Success - row contains the new revision
                self._circuit_breaker.record_success()
                return ModelMemoryExpireResult(
                    memory_id=command.memory_id,
                    success=True,
                    new_revision=row["lifecycle_revision"],
                    previous_state=EnumLifecycleState.ACTIVE,
                )

        except TimeoutError:
            self._circuit_breaker.record_timeout()
            logger.error(
                "Database query timeout for memory %s",
                command.memory_id,
            )
            return ModelMemoryExpireResult(
                memory_id=command.memory_id,
                success=False,
                conflict=False,
                error_message="Database query timeout",
            )
        except PostgresError as e:
            self._circuit_breaker.record_failure()
            logger.error(
                "Database error during expiration of memory %s: %s",
                command.memory_id,
                e,
            )
            return ModelMemoryExpireResult(
                memory_id=command.memory_id,
                success=False,
                conflict=False,
                error_message=f"Database error: {e}",
            )
        except (InterfaceError, InternalClientError) as e:
            # Handle asyncpg client-side errors:
            # - InterfaceError: Pool closing, connection already acquired, etc.
            # - InternalClientError: Protocol errors, schema cache issues, etc.
            self._circuit_breaker.record_failure()
            logger.error(
                "Client error during expiration of memory %s: %s",
                command.memory_id,
                e,
            )
            return ModelMemoryExpireResult(
                memory_id=command.memory_id,
                success=False,
                conflict=False,
                error_message=f"Client error: {e}",
            )

    async def handle_with_retry(
        self,
        memory_id: UUID,
        initial_revision: int,
        reason: str = "ttl_expired",
        expired_at: datetime | None = None,
    ) -> ModelMemoryExpireResult:
        """Handle expiration with automatic retry on conflict.

        Provides a higher-level API that automatically retries on revision
        conflicts by re-reading the current revision and retrying. This is
        useful when the caller doesn't need fine-grained control over retry
        behavior.

        Retry Strategy:
            - On conflict: re-read current state, retry with updated revision
            - On success or hard failure: return immediately
            - Bounded by max_retries to prevent infinite loops in high-contention scenarios

        Args:
            memory_id: UUID of the memory entity to expire.
            initial_revision: Starting revision for first attempt.
            reason: Reason for expiration (for audit trail).
            expired_at: Optional explicit expiration timestamp.

        Returns:
            ModelMemoryExpireResult with final outcome:
            - success=True: Expiration eventually succeeded
            - success=False, conflict=True: Max retries exceeded due to contention
            - success=False, conflict=False: Hard failure (invalid state, not found)

        Note:
            The retry logic is a domain policy decision that belongs in this
            handler. The underlying transaction helper (OMN-1524) will provide
            the connection and transaction primitives.
        """
        revision = initial_revision

        for attempt in range(self._max_retries):
            logger.debug(
                "Expiration attempt %d/%d for memory %s (revision=%d)",
                attempt + 1,
                self._max_retries,
                memory_id,
                revision,
            )

            result = await self.handle(
                ModelExpireMemoryCommand(
                    memory_id=memory_id,
                    expected_revision=revision,
                    reason=reason,
                    expired_at=expired_at,
                )
            )

            # Success or hard failure - return immediately
            if result.success or not result.conflict:
                return result

            # Conflict - re-read current revision for next attempt
            logger.info(
                "Conflict on attempt %d for memory %s, re-reading state",
                attempt + 1,
                memory_id,
            )

            try:
                current_state = await self._read_current_state(memory_id)
            except CircuitBreakerOpenError:
                logger.warning(
                    "Circuit breaker open during retry state read for memory %s",
                    memory_id,
                )
                return ModelMemoryExpireResult(
                    memory_id=memory_id,
                    success=False,
                    conflict=False,
                    error_message=(
                        "Database circuit breaker is open. "
                        "Unable to retry - service temporarily unavailable."
                    ),
                )
            except (PostgresError, InterfaceError, InternalClientError) as e:
                logger.error(
                    "Database error reading state during retry for memory %s: %s",
                    memory_id,
                    e,
                )
                return ModelMemoryExpireResult(
                    memory_id=memory_id,
                    success=False,
                    conflict=False,
                    error_message=f"Database error during retry state read: {e}",
                )
            except TimeoutError:
                logger.error(
                    "Timeout reading state during retry for memory %s",
                    memory_id,
                )
                return ModelMemoryExpireResult(
                    memory_id=memory_id,
                    success=False,
                    conflict=False,
                    error_message="Timeout during retry state read",
                )

            if current_state is None:
                return ModelMemoryExpireResult(
                    memory_id=memory_id,
                    success=False,
                    conflict=False,
                    error_message="Memory not found during retry",
                )

            # Check if memory is still in a valid state for expiration
            if current_state.lifecycle_state not in self._VALID_FROM_STATES:
                return ModelMemoryExpireResult(
                    memory_id=memory_id,
                    success=False,
                    conflict=False,
                    previous_state=current_state.lifecycle_state,
                    error_message=(
                        f"Memory transitioned to invalid state during retry: "
                        f"{current_state.lifecycle_state.value}"
                    ),
                )

            # Update revision for next attempt
            revision = current_state.lifecycle_revision

        # Max retries exceeded
        return ModelMemoryExpireResult(
            memory_id=memory_id,
            success=False,
            conflict=True,
            error_message=f"Max retries ({self._max_retries}) exceeded due to contention",
        )

    async def _read_current_state(
        self,
        memory_id: UUID,
    ) -> ModelMemoryCurrentState | None:
        """Read current state of a memory entity.

        Used by handle_with_retry() to get the updated revision after a
        conflict, and by handle() to provide detailed error information.

        Args:
            memory_id: UUID of the memory entity.

        Returns:
            ModelMemoryCurrentState if found, None if not found.

        Raises:
            RuntimeError: If handler is not initialized or database pool is not configured.
            PostgresError: If a database error occurs during the query.
            InterfaceError: If a client-side asyncpg error occurs.
            InternalClientError: If a protocol-level asyncpg error occurs.
            TimeoutError: If the database query exceeds the timeout.
        """
        # Require handler to be initialized
        if not self._initialized:
            raise RuntimeError(
                "Handler not initialized. Call initialize() before _read_current_state()."
            )

        # Require database pool for all operations (should always be true after init)
        if self._db_pool is None:
            raise RuntimeError(
                "Database pool not configured. "
                "Initialize handler with db_pool parameter."
            )

        # Type narrowing for circuit breaker (guaranteed set after initialize())
        assert self._circuit_breaker is not None

        # Check circuit breaker before attempting database operation
        if not self._circuit_breaker.should_allow_request():
            logger.warning(
                "Circuit breaker open, failing fast for state read of memory %s",
                memory_id,
            )
            raise CircuitBreakerOpenError(
                f"Circuit breaker is {self._circuit_breaker.state.value}"
            )

        try:
            async with self._db_pool.acquire() as conn:
                row = await asyncio.wait_for(
                    conn.fetchrow(self._READ_STATE_SQL, memory_id),
                    timeout=_QUERY_TIMEOUT_SECONDS,
                )

                if row is None:
                    self._circuit_breaker.record_success()
                    return None

                self._circuit_breaker.record_success()
                return ModelMemoryCurrentState(
                    memory_id=row["id"],
                    lifecycle_state=EnumLifecycleState(row["lifecycle_state"]),
                    lifecycle_revision=row["lifecycle_revision"],
                    updated_at=row["updated_at"],
                )
        except TimeoutError:
            self._circuit_breaker.record_timeout()
            logger.error(
                "Database query timeout reading state for memory %s",
                memory_id,
            )
            raise
        except PostgresError as e:
            self._circuit_breaker.record_failure()
            logger.error(
                "Database error reading state for memory %s: %s",
                memory_id,
                e,
            )
            raise
        except (InterfaceError, InternalClientError) as e:
            self._circuit_breaker.record_failure()
            logger.error(
                "Client error reading state for memory %s: %s",
                memory_id,
                e,
            )
            raise
