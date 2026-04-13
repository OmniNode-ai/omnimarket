# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2025 OmniNode Team
"""Handler for archiving memories to cold storage.

memories to filesystem archive with atomic writes and gzip compression.

Archive Format:
    - Format: JSONL (JSON Lines) with gzip compression (.jsonl.gz)
    - Partitioning: Date-based directory structure ({base}/{year}/{month}/{day}/)
    - Naming: {memory_id}.jsonl.gz

Atomic Write Pattern:
    The handler uses atomic writes to prevent partial/corrupt archives:
    1. Write compressed data to temporary file
    2. fsync to ensure durability
    3. Atomic rename to final path

    Note: Atomic write mechanics will be provided by OMN-1524 (infra primitive).
    Until then, this handler uses a local implementation.

Optimistic Locking:
    Uses expected_revision to prevent double-archive race conditions:
    1. Read memory with current revision
    2. Archive to filesystem
    3. Update DB state only if revision unchanged
    4. Return conflict=True if revision mismatch

Related Tickets:
    - OMN-1453: OmniMemory P4b - Lifecycle Orchestrator Database Integration
    - OMN-1524: Atomic write primitive (pending)

Example::

    from omnimarket.nodes.node_memory_lifecycle_orchestrator.handlers import (
        HandlerMemoryArchive,
        ModelArchiveMemoryCommand,
    )
    from pathlib import Path
    from uuid import UUID

    from omnibase_core.container import ModelONEXContainer

    # Create handler with container
    container = ModelONEXContainer()
    handler = HandlerMemoryArchive(container)

    # Option 1: Use environment variables (recommended)
    # export OMNIMEMORY_ARCHIVE_PATH=/var/omnimemory/archives
    # export OMNIMEMORY_ARCHIVE_COMPRESSION_LEVEL=6
    await handler.initialize(db_pool=pool)

    # Option 2: Explicit path and compression level (useful for testing)
    await handler.initialize(
        db_pool=pool,
        archive_base_path=Path("/custom/archive/path"),
        compression_level=1,  # Fast compression for high-throughput scenarios
    )

    command = ModelArchiveMemoryCommand(
        memory_id=UUID("abc12345-..."),
        expected_revision=5,
        archive_path=Path("/var/omnimemory/archives/2026/01/25/abc12345.jsonl.gz"),
    )

    result = await handler.handle(command)
    if result.success:
        print(f"Archived to {result.archive_path} ({result.bytes_written} bytes)")
    elif result.conflict:
        print("Revision conflict - memory was modified")
    else:
        print(f"Archive failed: {result.error_message}")

.. versionadded:: 0.1.0
    Initial implementation for OMN-1453.
"""

from __future__ import annotations

import asyncio
import contextlib
import gzip
import logging
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable
from uuid import UUID

from omnibase_core.models.metadata.model_generic_metadata import ModelGenericMetadata
from omnimemory.enums import EnumLifecycleState
from omnimemory.utils.concurrency import CircuitBreaker
from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from asyncpg import Pool
    from asyncpg.exceptions import InterfaceError, InternalClientError, PostgresError
    from omnibase_core.container import ModelONEXContainer
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

logger = logging.getLogger(__name__)

__all__ = [
    "HandlerMemoryArchive",
    "ModelArchiveMemoryCommand",
    "ModelArchiveRecord",
    "ModelMemoryArchiveHealth",
    "ModelMemoryArchiveMetadata",
    "ModelMemoryArchiveResult",
    "ProtocolOrphanedArchiveTracker",
]


@runtime_checkable
class ProtocolOrphanedArchiveTracker(Protocol):
    """Protocol for tracking orphaned archive files.

    An orphaned archive file occurs when the archive is successfully written
    to disk but the database state update fails (e.g., due to revision conflict
    or database error). These files exist on disk but are not tracked in the
    database, requiring periodic cleanup.

    Implementations of this protocol can:
    - Log orphaned files for later cleanup
    - Store in a dedicated cleanup queue
    - Send alerts for immediate investigation
    - Track metrics on orphan frequency

    Example::

        class FileOrphanTracker:
            async def track_orphan(
                self,
                memory_id: UUID,
                archive_path: Path,
                reason: str,
            ) -> None:
                with open("/var/log/orphans.jsonl", "a") as f:
                    f.write(json.dumps({
                        "memory_id": str(memory_id),
                        "archive_path": str(archive_path),
                        "reason": reason,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }) + "\\n")

    .. versionadded:: 0.1.0
        Added for orphan tracking in OMN-1453.
    """

    async def track_orphan(
        self,
        memory_id: UUID,
        archive_path: Path,
        reason: str,
    ) -> None:
        """Track an orphaned archive file for later cleanup.

        Called when an archive file is written but the database state
        cannot be updated, leaving the file orphaned.

        Args:
            memory_id: UUID of the memory that was archived.
            archive_path: Filesystem path where the orphaned archive exists.
            reason: Description of why the file was orphaned (e.g.,
                "revision_conflict_during_state_update" or
                "database_error_during_state_update").
        """
        ...


class ModelArchiveMemoryCommand(BaseModel):  # omnimemory-model-exempt: handler command
    """Command to archive a memory to cold storage.

    This command initiates the archival process for a specific memory entity.
    The expected_revision field enables optimistic locking to prevent race
    conditions during concurrent archive attempts.

    Attributes:
        memory_id: UUID of the memory entity to archive.
        expected_revision: Expected lifecycle revision for optimistic lock.
            If the actual revision differs, the archive operation fails
            with conflict=True to prevent double-archive.
        archive_path: Target filesystem path for the archive file.
            If not provided, the handler generates a path using date-based
            partitioning.
    """

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=True,
    )

    memory_id: UUID = Field(
        ...,
        description="ID of memory to archive",
    )
    expected_revision: int = Field(
        ...,
        ge=0,
        description="Expected revision for optimistic lock",
    )
    archive_path: Path | None = Field(
        default=None,
        description="Optional target archive file path (auto-generated if not provided)",
    )


class ModelMemoryArchiveResult(BaseModel):  # omnimemory-model-exempt: handler result
    """Result of an archive operation.

    Contains detailed information about the archive attempt, including
    success status, file location, and any error details.

    Attributes:
        memory_id: UUID of the archived memory.
        success: Whether the archive operation completed successfully.
        archived_at: Timestamp when the archive was created.
        archive_path: Filesystem path where the archive was written.
        bytes_written: Number of compressed bytes written to the archive.
        conflict: True if a revision conflict prevented archival.
        orphaned: True if an archive file was written but database state
            update failed, leaving an orphaned file on disk.
        error_message: Human-readable error description if failed.
    """

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=True,
    )

    memory_id: UUID = Field(
        ...,
        description="ID of the archived memory",
    )
    success: bool = Field(
        ...,
        description="Whether the archive operation succeeded",
    )
    archived_at: datetime | None = Field(
        default=None,
        description="Timestamp of successful archive",
    )
    archive_path: Path | None = Field(
        default=None,
        description="Path to the archive file",
    )
    bytes_written: int = Field(
        default=0,
        ge=0,
        description="Number of compressed bytes written",
    )
    conflict: bool = Field(
        default=False,
        description="True if revision conflict prevented archival",
    )
    orphaned: bool = Field(
        default=False,
        description="True if archive file was written but DB state update failed",
    )
    error_message: str | None = Field(
        default=None,
        description="Error details if archive failed",
    )


class ModelArchiveRecord(BaseModel):  # omnimemory-model-exempt: archive record format
    """Record format for archived memory.

    This model defines the schema for archived memory records. Each archive
    file contains one JSONL record (one JSON object per line), compressed
    with gzip.

    The archive_version field enables future schema migrations while
    maintaining backwards compatibility with existing archives.

    Attributes:
        memory_id: UUID of the archived memory.
        content: The memory content (text, structured data, etc.).
        content_type: MIME type or content classification.
        created_at: When the memory was originally created.
        expired_at: When the memory transitioned to EXPIRED state.
        archived_at: When the memory was archived to cold storage.
        lifecycle_revision: The revision number at time of archival.
        archive_version: Schema version for archive format migrations.
        metadata: Optional additional metadata from the memory.
    """

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=True,
    )

    memory_id: UUID = Field(
        ...,
        description="UUID of the archived memory",
    )
    content: str = Field(
        ...,
        description="The memory content",
    )
    content_type: str = Field(
        ...,
        description="MIME type or content classification",
    )
    created_at: datetime = Field(
        ...,
        description="When the memory was originally created",
    )
    expired_at: datetime = Field(
        ...,
        description="When the memory transitioned to EXPIRED state",
    )
    archived_at: datetime = Field(
        ...,
        description="When the memory was archived to cold storage",
    )
    lifecycle_revision: int = Field(
        ...,
        ge=0,
        description="The revision number at time of archival",
    )
    archive_version: str = Field(
        default="1.0",
        description="Schema version for archive format migrations",
    )
    metadata: ModelGenericMetadata | None = Field(
        default=None,
        description="Optional additional metadata from the memory",
    )


class ModelMemoryRow(BaseModel):  # omnimemory-model-exempt: handler internal
    """Internal model for memory row data from database.

    Used internally by the handler to represent memory data fetched
    from the database before archival.
    """

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=True,
    )

    id: UUID
    content: str
    content_type: str
    created_at: datetime
    expired_at: datetime | None
    lifecycle_state: EnumLifecycleState
    lifecycle_revision: int
    metadata: ModelGenericMetadata | None = None


class ModelCircuitBreakerConfigInfo(  # omnimemory-model-exempt: handler metadata
    BaseModel
):
    """Circuit breaker configuration info for handler metadata.

    Attributes:
        failure_threshold: Number of failures before opening circuit.
        recovery_timeout: Seconds to wait before attempting recovery.
        success_threshold: Successes needed to close circuit.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    failure_threshold: int = Field(
        ...,
        description="Number of failures before opening circuit",
    )
    recovery_timeout: float = Field(
        ...,
        description="Seconds to wait before attempting recovery",
    )
    success_threshold: int = Field(
        ...,
        description="Successes needed to close circuit",
    )


class ModelMemoryArchiveHealth(BaseModel):  # omnimemory-model-exempt: handler health
    """Health status for the Memory Archive Handler.

    Returned by health_check() to provide detailed health information
    about the handler and its dependencies.

    Attributes:
        initialized: Whether the handler has been initialized.
        db_pool_available: Whether a database connection pool is configured.
        archive_base_path: The configured archive base path.
        orphan_tracker_configured: Whether an orphan tracker is configured.
        circuit_breaker_state: Current state of the database circuit breaker.
    """

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=True,
    )

    initialized: bool = Field(
        ...,
        description="Whether the handler has been initialized",
    )
    db_pool_available: bool = Field(
        ...,
        description="Whether a database connection pool is configured",
    )
    archive_base_path: str | None = Field(
        default=None,
        description="The configured archive base path",
    )
    orphan_tracker_configured: bool = Field(
        ...,
        description="Whether an orphan tracker is configured for handling orphaned archives",
    )
    circuit_breaker_state: str = Field(
        ...,
        description="Current state of the database circuit breaker (closed, open, half_open, not_configured)",
    )


class ModelMemoryArchiveMetadata(  # omnimemory-model-exempt: handler metadata
    BaseModel
):
    """Metadata describing memory archive handler capabilities and configuration.

    Returned by describe() method to provide introspection information
    about the handler's purpose, capabilities, archive format, and configuration.

    Attributes:
        name: Handler class name.
        description: Brief description of handler purpose.
        capabilities: List of supported capabilities.
        archive_format: Description of the archive file format.
        compression_level: Configured gzip compression level.
        query_timeout_seconds: Database query timeout in seconds.
        circuit_breaker_config: Circuit breaker configuration.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

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
    archive_format: str = Field(
        ...,
        description="Description of the archive file format",
    )
    compression_level: int = Field(
        ...,
        ge=1,
        le=9,
        description="Configured gzip compression level (1-9)",
    )
    query_timeout_seconds: float = Field(
        ...,
        description="Database query timeout in seconds",
    )
    circuit_breaker_config: ModelCircuitBreakerConfigInfo = Field(
        ...,
        description="Circuit breaker configuration",
    )


class HandlerMemoryArchive:
    """Handler for archiving memories to cold storage.

    This handler performs the complete archival workflow:

    1. **Read memory content** from database with optimistic lock check
    2. **Validate state** - only EXPIRED memories can be archived
    3. **Serialize** to archive format (JSONL)
    4. **Compress** with gzip (domain decision - compression format owned here)
    5. **Write atomically** (temp file + rename) - uses infra primitive
    6. **Update database** state: EXPIRED -> ARCHIVED

    Archive Directory Structure::

        {archive_base_path}/
            2026/
                01/
                    25/
                        {memory_id_1}.jsonl.gz
                        {memory_id_2}.jsonl.gz
                    26/
                        {memory_id_3}.jsonl.gz

    Thread Safety:
        This handler is stateless and safe for concurrent use. Each archive
        operation is independent and uses optimistic locking to handle races.

    Attributes:
        archive_base_path: Base directory for archive storage.

    Note:
        Atomic write mechanics will be provided by OMN-1524 (infra primitive).
        The current implementation uses a local atomic write pattern.
    """

    # Default gzip compression level for archive files (valid range: 1-9).
    #
    # Level 6 is the gzip default and provides an optimal balance between
    # compression ratio and CPU time for archive storage workloads:
    #   - Levels 1-3: Faster compression but lower ratio (~20-30% savings)
    #   - Level 6: Balanced - good ratio (~60-70% savings) with moderate CPU
    #   - Levels 7-9: Higher ratio (~70-75% savings) but significantly slower
    #
    # For cold storage archives where read latency is acceptable and storage
    # cost matters, level 6 optimizes throughput while maintaining substantial
    # space savings. Higher levels provide diminishing returns for JSON/JSONL
    # content which already compresses well.
    #
    # Override via constructor or OMNIMEMORY_ARCHIVE_COMPRESSION_LEVEL env var.
    _DEFAULT_COMPRESSION_LEVEL: int = 6

    # Minimum and maximum valid gzip compression levels.
    _MIN_COMPRESSION_LEVEL: int = 1
    _MAX_COMPRESSION_LEVEL: int = 9

    # Query timeout for database operations (seconds).
    #
    # This timeout applies to individual database queries (SELECT, UPDATE),
    # not connection acquisition. It prevents indefinite blocking when the
    # database is under heavy load or experiencing issues.
    _QUERY_TIMEOUT_SECONDS: float = 30.0

    # Circuit breaker configuration defaults.
    #
    # These defaults balance responsiveness with stability:
    # - failure_threshold=5: Opens after 5 consecutive failures
    # - recovery_timeout=60: Waits 60s before attempting recovery
    # - success_threshold=2: Requires 2 successes to fully close
    _CB_FAILURE_THRESHOLD: int = 5
    _CB_RECOVERY_TIMEOUT: float = 60.0
    _CB_SUCCESS_THRESHOLD: int = 2

    def __init__(self, container: ModelONEXContainer) -> None:
        """Initialize the archive handler with a dependency container.

        Args:
            container: ONEX dependency injection container for service resolution.
                The container follows the ONEX DI pattern where handlers receive
                all dependencies through the container rather than as constructor
                parameters.

        Note:
            After construction, call initialize() to set up runtime dependencies
            (db_pool, archive_base_path, orphan_tracker, compression_level).
            The handler will raise RuntimeError if handle() is called before
            initialization.
        """
        self._container = container
        self._db_pool: Pool | None = None
        self._archive_base_path: Path | None = None
        self._orphan_tracker: ProtocolOrphanedArchiveTracker | None = None
        self._db_circuit_breaker: CircuitBreaker | None = None
        self._compression_level: int = self._DEFAULT_COMPRESSION_LEVEL
        self._initialized = False

    async def initialize(
        self,
        db_pool: Pool | None = None,
        archive_base_path: Path | None = None,
        orphan_tracker: ProtocolOrphanedArchiveTracker | None = None,
        compression_level: int | None = None,
    ) -> None:
        """Initialize runtime dependencies for the handler.

        This method must be called after construction and before handle() to
        set up the handler's runtime dependencies. This pattern separates
        construction (container injection) from initialization (runtime setup).

        Args:
            db_pool: PostgreSQL connection pool for database operations.
                If None, database operations will raise RuntimeError.
            archive_base_path: Base directory for archive storage.
                If None, reads from OMNIMEMORY_ARCHIVE_PATH environment variable.
                If env var not set, falls back to {tempdir}/omnimemory/archives.
                Directories are created on-demand during archive operations.
            orphan_tracker: Optional tracker for orphaned archive files.
                If provided, will be called when an archive file is written
                but the database state update fails. This enables monitoring
                and cleanup of orphaned files.
            compression_level: Gzip compression level (1-9). Level 1 is fastest
                with lowest ratio; level 9 is slowest with highest ratio. Level
                6 is the balanced default.
                Resolution order (first non-None value wins):
                  1. ``compression_level`` argument (this parameter)
                  2. ``OMNIMEMORY_ARCHIVE_COMPRESSION_LEVEL`` environment variable
                  3. Built-in default (6)

        Raises:
            ValueError: If ``compression_level`` (the argument) is outside
                the valid range [1, 9], OR if the
                ``OMNIMEMORY_ARCHIVE_COMPRESSION_LEVEL`` environment variable
                is set but is not a valid integer (e.g. ``"fast"``), OR if
                the integer parsed from the env var is outside [1, 9].
        """
        self._db_pool = db_pool
        self._orphan_tracker = orphan_tracker

        # Resolve compression level: explicit arg > env var > default
        if compression_level is not None:
            resolved_level = compression_level
            level_source = "constructor argument"
        else:
            env_level_raw = os.environ.get("OMNIMEMORY_ARCHIVE_COMPRESSION_LEVEL")
            if env_level_raw is not None:
                try:
                    resolved_level = int(env_level_raw)
                except ValueError:
                    raise ValueError(
                        "OMNIMEMORY_ARCHIVE_COMPRESSION_LEVEL must be an integer "
                        f"between {self._MIN_COMPRESSION_LEVEL} and "
                        f"{self._MAX_COMPRESSION_LEVEL} (inclusive), "
                        f"got non-integer value: {env_level_raw!r}"
                    ) from None
                level_source = "OMNIMEMORY_ARCHIVE_COMPRESSION_LEVEL env var"
            else:
                resolved_level = self._DEFAULT_COMPRESSION_LEVEL
                level_source = "built-in default"

        # Validate range
        if not (
            self._MIN_COMPRESSION_LEVEL <= resolved_level <= self._MAX_COMPRESSION_LEVEL
        ):
            raise ValueError(
                f"compression_level must be between {self._MIN_COMPRESSION_LEVEL} "
                f"and {self._MAX_COMPRESSION_LEVEL} (inclusive), "
                f"got {resolved_level} (from {level_source})"
            )

        self._compression_level = resolved_level
        logger.debug(
            "Archive compression level configured",
            extra={
                "compression_level": self._compression_level,
                "source": level_source,
            },
        )

        # Initialize circuit breaker for DB operations
        self._db_circuit_breaker = CircuitBreaker(
            failure_threshold=self._CB_FAILURE_THRESHOLD,
            recovery_timeout=self._CB_RECOVERY_TIMEOUT,
            success_threshold=self._CB_SUCCESS_THRESHOLD,
        )

        if archive_base_path is not None:
            self._archive_base_path = archive_base_path
        else:
            env_path = os.environ.get("OMNIMEMORY_ARCHIVE_PATH")
            if env_path:
                self._archive_base_path = Path(env_path)
                logger.debug(
                    "Using archive path from OMNIMEMORY_ARCHIVE_PATH",
                    extra={"archive_path": str(self._archive_base_path)},
                )
            else:
                self._archive_base_path = (
                    Path(tempfile.gettempdir()) / "omnimemory" / "archives"
                )
                logger.info(
                    "OMNIMEMORY_ARCHIVE_PATH not set, using default archive path",
                    extra={"archive_path": str(self._archive_base_path)},
                )

        self._initialized = True

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
            self._archive_base_path = None
            self._orphan_tracker = None
            self._db_circuit_breaker = None
            self._compression_level = self._DEFAULT_COMPRESSION_LEVEL
            self._initialized = False
            logger.info("HandlerMemoryArchive shutdown complete")

    async def health_check(self) -> ModelMemoryArchiveHealth:
        """Check the health status of the handler.

        Returns:
            ModelMemoryArchiveHealth with detailed status information:
            - initialized: Whether the handler has been initialized
            - db_pool_available: Whether a database pool is configured
            - archive_base_path: The configured archive base path
            - orphan_tracker_configured: Whether an orphan tracker is configured
            - circuit_breaker_state: Current state of the DB circuit breaker
        """
        circuit_state = "not_configured"
        if self._db_circuit_breaker is not None:
            circuit_state = self._db_circuit_breaker.state.value

        return ModelMemoryArchiveHealth(
            initialized=self._initialized,
            db_pool_available=self._db_pool is not None,
            archive_base_path=str(self._archive_base_path)
            if self._archive_base_path
            else None,
            orphan_tracker_configured=self._orphan_tracker is not None,
            circuit_breaker_state=circuit_state,
        )

    async def describe(self) -> ModelMemoryArchiveMetadata:
        """Return metadata and capabilities of this handler.

        Provides introspection information about the handler, including
        its purpose, supported operations, and configuration.

        Returns:
            ModelMemoryArchiveMetadata with handler information including
            name, description, capabilities, and archive configuration.
        """
        return ModelMemoryArchiveMetadata(
            name="HandlerMemoryArchive",
            description=(
                "Archives EXPIRED memories to cold storage with gzip compression "
                "and atomic writes. Uses optimistic locking for concurrency safety."
            ),
            capabilities=[
                "archive_expired_memory",
                "atomic_file_write",
                "optimistic_locking",
                "orphan_tracking",
            ],
            archive_format="JSONL with gzip compression (.jsonl.gz)",
            compression_level=self._compression_level,
            query_timeout_seconds=self._QUERY_TIMEOUT_SECONDS,
            circuit_breaker_config=ModelCircuitBreakerConfigInfo(
                failure_threshold=self._CB_FAILURE_THRESHOLD,
                recovery_timeout=self._CB_RECOVERY_TIMEOUT,
                success_threshold=self._CB_SUCCESS_THRESHOLD,
            ),
        )

    @property
    def initialized(self) -> bool:
        """Check if the handler has been initialized.

        Returns:
            True if initialize() has been called successfully.
        """
        return self._initialized

    @property
    def archive_base_path(self) -> Path | None:
        """Get the base path for archives.

        Returns:
            The configured archive base path, or None if not yet initialized.
        """
        return self._archive_base_path

    @property
    def compression_level(self) -> int:
        """Get the configured gzip compression level.

        Returns:
            The active compression level (1-9). Returns the instance value,
            which reflects the resolved configuration from constructor argument,
            environment variable, or built-in default.
        """
        return self._compression_level

    async def handle(
        self,
        command: ModelArchiveMemoryCommand,
    ) -> ModelMemoryArchiveResult:
        """Handle an archive command.

        Performs the complete archival workflow with optimistic locking
        to prevent race conditions.

        Args:
            command: Archive command with memory ID and expected revision.

        Returns:
            Result indicating success, conflict, or failure with details.

        Raises:
            RuntimeError: If handler is not initialized or database pool is not configured.
        """
        # Require handler to be initialized
        if not self._initialized:
            raise RuntimeError(
                "Handler not initialized. Call initialize() before handle()."
            )

        now = datetime.now(UTC)

        # Explicit guard for circuit breaker (guaranteed set after initialize())
        # Note: Using explicit guard instead of assert because assertions can be
        # disabled in production with -O flag, making this a critical path check.
        if self._db_circuit_breaker is None:
            raise RuntimeError(
                "Circuit breaker not initialized. This indicates a bug in initialize()."
            )

        # Check circuit breaker before any DB operations
        if not self._db_circuit_breaker.should_allow_request():
            logger.warning(
                "Circuit breaker open, rejecting archive request",
                extra={
                    "memory_id": str(command.memory_id),
                    "circuit_state": self._db_circuit_breaker.state.value,
                },
            )
            return ModelMemoryArchiveResult(
                memory_id=command.memory_id,
                success=False,
                error_message=(
                    "Database circuit breaker is open. "
                    "Service is protecting against cascading failures."
                ),
            )

        # Read memory with optimistic lock check: returns None on revision mismatch
        try:
            memory = await self._read_memory(
                command.memory_id,
                command.expected_revision,
            )
            # Record successful DB read (only for non-None results from actual DB query)
            if memory is not None:
                self._db_circuit_breaker.record_success()
        except ValueError as e:
            # ValueError indicates memory not found - this is not a DB failure
            return ModelMemoryArchiveResult(
                memory_id=command.memory_id,
                success=False,
                error_message=str(e),
            )
        except PostgresError as e:
            # Handle asyncpg database errors (query errors, constraint violations, etc.)
            self._db_circuit_breaker.record_failure()
            logger.error(
                "Database error reading memory",
                extra={
                    "memory_id": str(command.memory_id),
                    "error": str(e),
                    "error_type": type(e).__name__,
                },
            )
            return ModelMemoryArchiveResult(
                memory_id=command.memory_id,
                success=False,
                error_message=f"Database error reading memory: {e}",
            )
        except (InterfaceError, InternalClientError) as e:
            # Handle asyncpg client-side errors not covered by PostgresError:
            # - InterfaceError: Pool closing, connection already acquired, etc.
            # - InternalClientError: Protocol errors, schema cache issues, etc.
            self._db_circuit_breaker.record_failure()
            logger.error(
                "Client error reading memory",
                extra={
                    "memory_id": str(command.memory_id),
                    "error": str(e),
                    "error_type": type(e).__name__,
                },
            )
            return ModelMemoryArchiveResult(
                memory_id=command.memory_id,
                success=False,
                error_message=f"Client error reading memory: {e}",
            )
        except TimeoutError as e:
            self._db_circuit_breaker.record_timeout()
            logger.error(
                "Query timeout reading memory",
                extra={
                    "memory_id": str(command.memory_id),
                    "error": str(e),
                },
            )
            return ModelMemoryArchiveResult(
                memory_id=command.memory_id,
                success=False,
                error_message=f"Query timeout reading memory: {e}",
            )

        if memory is None:
            return ModelMemoryArchiveResult(
                memory_id=command.memory_id,
                success=False,
                conflict=True,
                error_message=(
                    f"Revision conflict: expected {command.expected_revision}, "
                    "memory was modified or not found"
                ),
            )

        # Guard: only EXPIRED memories may be archived; reject any other lifecycle state
        if memory.lifecycle_state != EnumLifecycleState.EXPIRED:
            return ModelMemoryArchiveResult(
                memory_id=command.memory_id,
                success=False,
                error_message=(
                    f"Cannot archive memory in state {memory.lifecycle_state.value}. "
                    "Only EXPIRED memories can be archived."
                ),
            )

        # Build immutable archive record from memory snapshot before any I/O
        record = ModelArchiveRecord(
            memory_id=memory.id,
            content=memory.content,
            content_type=memory.content_type,
            created_at=memory.created_at,
            expired_at=memory.expired_at or now,  # Fallback if not set
            archived_at=now,
            lifecycle_revision=memory.lifecycle_revision,
            metadata=memory.metadata,
        )

        # Offload compression to thread pool to avoid blocking the event loop on large payloads
        compressed_bytes = await self._serialize_for_archive_async(record)

        archive_path = command.archive_path or self._get_archive_path(
            command.memory_id,
            now,
        )

        # Validate custom archive paths to prevent directory traversal attacks.
        # Generated paths (via _get_archive_path) are always within the base; only
        # caller-supplied paths need validation.
        if command.archive_path is not None:
            validation_error = self._validate_archive_path(command.archive_path)
            if validation_error is not None:
                logger.warning(
                    "Rejected archive path outside allowed directory",
                    extra={
                        "memory_id": str(command.memory_id),
                        "requested_path": str(command.archive_path),
                        "archive_base_path": str(self._archive_base_path),
                    },
                )
                return ModelMemoryArchiveResult(
                    memory_id=command.memory_id,
                    success=False,
                    error_message=validation_error,
                )

        # Atomic write: temp file → fsync → rename (prevents partial/corrupt archives)
        try:
            bytes_written = await self._write_archive_atomic(
                archive_path,
                compressed_bytes,
            )
        except OSError as e:
            logger.error(
                "Failed to write archive",
                extra={
                    "memory_id": str(command.memory_id),
                    "archive_path": str(archive_path),
                    "error": str(e),
                    "error_type": type(e).__name__,
                },
            )
            return ModelMemoryArchiveResult(
                memory_id=command.memory_id,
                success=False,
                error_message=f"Archive write failed: {e}",
            )

        # Update DB state AFTER successful filesystem write — archive file is the source of truth
        try:
            updated = await self._mark_archived(
                command.memory_id,
                command.expected_revision,
                now,
                archive_path,
            )
            # Record successful DB operation
            # Note: We record success even on revision conflict because
            # the DB query itself succeeded - conflict is application logic
            self._db_circuit_breaker.record_success()

            if not updated:
                # Revision conflict during state update
                # Note: Archive file was written but state not updated
                # This is a known edge case - the file exists but memory
                # may be re-archived. Idempotent archive format handles this.
                logger.warning(
                    "Revision conflict during state update",
                    extra={
                        "memory_id": str(command.memory_id),
                        "archive_path": str(archive_path),
                    },
                )

                # Track orphaned file if tracker configured
                if self._orphan_tracker is not None:
                    await self._orphan_tracker.track_orphan(
                        memory_id=command.memory_id,
                        archive_path=archive_path,
                        reason="revision_conflict_during_state_update",
                    )

                return ModelMemoryArchiveResult(
                    memory_id=command.memory_id,
                    success=False,
                    conflict=True,
                    orphaned=True,
                    archive_path=archive_path,
                    bytes_written=bytes_written,
                    error_message=(
                        "Revision conflict during state update. "
                        "Archive file written but state not updated."
                    ),
                )
        except PostgresError as e:
            self._db_circuit_breaker.record_failure()
            logger.error(
                "Database error updating state",
                extra={
                    "memory_id": str(command.memory_id),
                    "error": str(e),
                    "error_type": type(e).__name__,
                },
            )

            # Track orphaned file if tracker configured
            if self._orphan_tracker is not None:
                await self._orphan_tracker.track_orphan(
                    memory_id=command.memory_id,
                    archive_path=archive_path,
                    reason="database_error_during_state_update",
                )

            return ModelMemoryArchiveResult(
                memory_id=command.memory_id,
                success=False,
                orphaned=True,
                archive_path=archive_path,
                bytes_written=bytes_written,
                error_message=f"Database error during state update: {e}",
            )
        except (InterfaceError, InternalClientError) as e:
            # Handle asyncpg client-side errors not covered by PostgresError:
            # - InterfaceError: Pool closing, connection already acquired, etc.
            # - InternalClientError: Protocol errors, schema cache issues, etc.
            self._db_circuit_breaker.record_failure()
            logger.error(
                "Client error updating state",
                extra={
                    "memory_id": str(command.memory_id),
                    "error": str(e),
                    "error_type": type(e).__name__,
                },
            )

            # Track orphaned file if tracker configured
            if self._orphan_tracker is not None:
                await self._orphan_tracker.track_orphan(
                    memory_id=command.memory_id,
                    archive_path=archive_path,
                    reason="client_error_during_state_update",
                )

            return ModelMemoryArchiveResult(
                memory_id=command.memory_id,
                success=False,
                orphaned=True,
                archive_path=archive_path,
                bytes_written=bytes_written,
                error_message=f"Client error during state update: {e}",
            )
        except TimeoutError as e:
            # Handle pool acquisition or query timeout
            self._db_circuit_breaker.record_timeout()
            logger.error(
                "Timeout updating state",
                extra={
                    "memory_id": str(command.memory_id),
                    "error": str(e),
                },
            )

            # Track orphaned file if tracker configured
            if self._orphan_tracker is not None:
                await self._orphan_tracker.track_orphan(
                    memory_id=command.memory_id,
                    archive_path=archive_path,
                    reason="timeout_during_state_update",
                )

            return ModelMemoryArchiveResult(
                memory_id=command.memory_id,
                success=False,
                orphaned=True,
                archive_path=archive_path,
                bytes_written=bytes_written,
                error_message=f"Timeout during state update: {e}",
            )

        logger.info(
            "Successfully archived memory",
            extra={
                "memory_id": str(command.memory_id),
                "archive_path": str(archive_path),
                "bytes_written": bytes_written,
                "lifecycle_revision": command.expected_revision,
            },
        )

        return ModelMemoryArchiveResult(
            memory_id=command.memory_id,
            success=True,
            archived_at=now,
            archive_path=archive_path,
            bytes_written=bytes_written,
        )

    def _get_archive_path(self, memory_id: UUID, archived_at: datetime) -> Path:
        """Generate archive path with date-based partitioning.

        Creates a hierarchical directory structure based on the archive date
        to enable efficient browsing and cleanup of old archives.

        Pattern: {base}/{year}/{month:02d}/{day:02d}/{memory_id}.jsonl.gz

        Args:
            memory_id: UUID of the memory being archived.
            archived_at: Timestamp of the archive operation.

        Returns:
            Path to the archive file.

        Example:
            >>> handler._get_archive_path(
            ...     UUID("abc12345-..."),
            ...     datetime(2026, 1, 25, 10, 30, 0),
            ... )
            Path("/var/omnimemory/archives/2026/01/25/abc12345-....jsonl.gz")
        """
        # Explicit guard for archive base path (guaranteed set after initialize())
        # Note: Using explicit guard instead of assert because assertions can be
        # disabled in production with -O flag, making this a critical path check.
        if self._archive_base_path is None:
            raise RuntimeError(
                "Archive base path not initialized. "
                "This indicates a bug in initialize()."
            )

        return (
            self._archive_base_path
            / str(archived_at.year)
            / f"{archived_at.month:02d}"
            / f"{archived_at.day:02d}"
            / f"{memory_id}.jsonl.gz"
        )

    def _validate_archive_path(self, archive_path: Path) -> str | None:
        """Validate that an archive path is within the allowed base directory.

        Prevents directory traversal attacks (e.g., ../../../etc/passwd) by
        ensuring the resolved path is under the configured archive base path.

        Security Note:
            This method uses Path.resolve() to normalize the path and eliminate
            any '..' components, symlinks, or other path manipulation attempts.
            The resolved path must start with the resolved base path.

        Args:
            archive_path: The path to validate (may be user-provided).

        Returns:
            None if the path is valid, or an error message string if invalid.
        """
        # Explicit guard for archive base path
        if self._archive_base_path is None:
            return (
                "Archive base path not initialized. "
                "Call initialize() before validating paths."
            )

        # Resolve both paths to eliminate '..' components and symlinks
        try:
            # Resolve the base path (should already exist or will be created)
            resolved_base = self._archive_base_path.resolve()

            # For the archive path, we need to handle the case where parent
            # directories don't exist yet. We resolve what we can.
            # Note: resolve() on a non-existent path returns the normalized
            # absolute path without following symlinks for non-existent parts.
            resolved_archive = archive_path.resolve()

            # Check if the resolved archive path is under the resolved base path
            # Using is_relative_to() for clean path containment check (Python 3.9+)
            if not resolved_archive.is_relative_to(resolved_base):
                return (
                    f"Archive path '{archive_path}' is outside allowed directory. "
                    f"Path must be under '{self._archive_base_path}'."
                )

            return None  # Path is valid

        except (OSError, ValueError) as e:
            # OSError: Path resolution failed (e.g., permission denied on symlink)
            # ValueError: Path operations failed (e.g., invalid path characters)
            return f"Failed to validate archive path: {e}"

    def _serialize_for_archive_sync(self, record: ModelArchiveRecord) -> bytes:
        """Serialize record to compressed JSONL bytes (synchronous).

        This is a synchronous CPU-bound operation that performs gzip compression.
        For large payloads, use _serialize_for_archive_async() to avoid blocking
        the event loop.

        The domain owns the format decision (gzip + JSONL).
        Infrastructure will own atomic write mechanics (OMN-1524).

        Compression is applied here because:
        1. Archive format is a domain decision
        2. Compression ratio for JSON is significant (typically 5-10x)
        3. Keeping compression in domain allows format-specific optimization

        Args:
            record: The archive record to serialize.

        Returns:
            Gzip-compressed JSONL bytes.
        """
        jsonl_line = record.model_dump_json() + "\n"
        return gzip.compress(
            jsonl_line.encode("utf-8"),
            compresslevel=self._compression_level,
        )

    async def _serialize_for_archive_async(
        self,
        record: ModelArchiveRecord,
    ) -> bytes:
        """Serialize record to compressed JSONL bytes (async, non-blocking).

        Offloads the CPU-bound gzip compression to a thread pool to avoid
        blocking the event loop. This is recommended for large payloads.

        Args:
            record: The archive record to serialize.

        Returns:
            Gzip-compressed JSONL bytes.
        """
        return await asyncio.to_thread(
            self._serialize_for_archive_sync,
            record,
        )

    def _write_archive_sync(
        self,
        archive_path: Path,
        compressed_bytes: bytes,
    ) -> int:
        """Synchronous atomic write - runs in thread pool.

        Uses the temp file + rename pattern to ensure atomic writes:
        1. Create parent directories if needed
        2. Write to temporary file in same directory
        3. fsync to ensure durability
        4. Atomic rename to final path

        This method performs blocking I/O and should be called via
        asyncio.to_thread() from async contexts.

        Note: This will be replaced by omnibase_infra.write_atomic_bytes()
        when OMN-1524 is implemented.

        Args:
            archive_path: Target path for the archive file.
            compressed_bytes: Compressed archive data to write.

        Returns:
            Number of bytes written.

        Raises:
            OSError: If directory creation or file write fails.
        """
        # Ensure parent directory exists
        archive_path.parent.mkdir(parents=True, exist_ok=True)

        # Write to temp file, then atomic rename
        # Using same directory ensures rename is atomic (same filesystem)
        fd, temp_path = tempfile.mkstemp(
            suffix=".tmp",
            prefix=f"{archive_path.stem}.",
            dir=archive_path.parent,
        )

        try:
            # Write compressed data
            os.write(fd, compressed_bytes)
            # Ensure data is flushed to disk
            os.fsync(fd)
            os.close(fd)
            fd = -1  # Mark as closed

            # Atomic rename
            Path(temp_path).rename(archive_path)

            return len(compressed_bytes)

        except (OSError, ValueError):
            # Cleanup temp file on failure
            # OSError: covers all filesystem errors (write, fsync, rename, unlink)
            # ValueError: invalid file descriptor operations
            if fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(fd)
            temp_path_obj = Path(temp_path)
            try:
                if temp_path_obj.exists():
                    temp_path_obj.unlink()
            except OSError:
                pass  # Ignore unlink errors during cleanup
            raise

    async def _write_archive_atomic(
        self,
        archive_path: Path,
        compressed_bytes: bytes,
    ) -> int:
        """Write archive file atomically using thread pool.

        Delegates to _write_archive_sync via asyncio.to_thread() to avoid
        blocking the event loop during file I/O operations.

        Args:
            archive_path: Target path for the archive file.
            compressed_bytes: Compressed archive data to write.

        Returns:
            Number of bytes written.

        Raises:
            OSError: If directory creation or file write fails.
        """
        return await asyncio.to_thread(
            self._write_archive_sync,
            archive_path,
            compressed_bytes,
        )

    async def _read_memory(
        self,
        memory_id: UUID,
        expected_revision: int,
    ) -> ModelMemoryRow | None:
        """Read memory from database with optimistic lock check.

        Fetches the memory entity and validates that its revision matches
        the expected revision. Returns None if the revision doesn't match,
        indicating a concurrent modification.

        Args:
            memory_id: UUID of the memory to read.
            expected_revision: Expected lifecycle_revision value.

        Returns:
            Memory row if found and revision matches, None otherwise.

        Raises:
            RuntimeError: If database pool is not configured.
            ValueError: If memory is not found.
            TimeoutError: If database query exceeds timeout.
        """
        if self._db_pool is None:
            raise RuntimeError(
                "Database pool not configured. "
                "Initialize handler with db_pool parameter."
            )

        async with self._db_pool.acquire() as conn:
            row = await asyncio.wait_for(
                conn.fetchrow(
                    """
                    SELECT
                        id,
                        content,
                        content_type,
                        created_at,
                        expired_at,
                        lifecycle_state,
                        lifecycle_revision,
                        metadata
                    FROM memories
                    WHERE id = $1
                    """,
                    memory_id,
                ),
                timeout=self._QUERY_TIMEOUT_SECONDS,
            )

            if row is None:
                raise ValueError(f"Memory {memory_id} not found")

            # Check revision matches
            if row["lifecycle_revision"] != expected_revision:
                logger.debug(
                    "Revision mismatch for memory",
                    extra={
                        "memory_id": str(memory_id),
                        "expected_revision": expected_revision,
                        "actual_revision": row["lifecycle_revision"],
                    },
                )
                return None

            # Convert raw metadata dict to ModelGenericMetadata if present
            raw_metadata = row["metadata"]
            metadata: ModelGenericMetadata | None = None
            if raw_metadata is not None:
                metadata = ModelGenericMetadata.model_validate(raw_metadata)

            return ModelMemoryRow(
                id=row["id"],
                content=row["content"],
                content_type=row["content_type"],
                created_at=row["created_at"],
                expired_at=row["expired_at"],
                lifecycle_state=EnumLifecycleState(row["lifecycle_state"]),
                lifecycle_revision=row["lifecycle_revision"],
                metadata=metadata,
            )

    async def _mark_archived(
        self,
        memory_id: UUID,
        expected_revision: int,
        archived_at: datetime,
        archive_path: Path,
    ) -> bool:
        """Update memory state to ARCHIVED with optimistic locking.

        Performs an atomic update that only succeeds if the current
        revision matches the expected revision. Increments the revision
        on successful update.

        Args:
            memory_id: UUID of the memory to update.
            expected_revision: Expected current revision (optimistic lock).
            archived_at: Timestamp of the archive operation.
            archive_path: Path where the archive was written.

        Returns:
            True if update succeeded, False if revision conflict.

        Raises:
            RuntimeError: If database pool is not configured.
            TimeoutError: If database query exceeds timeout.
        """
        if self._db_pool is None:
            raise RuntimeError(
                "Database pool not configured. "
                "Initialize handler with db_pool parameter."
            )

        async with self._db_pool.acquire() as conn:
            result = await asyncio.wait_for(
                conn.execute(
                    """
                    UPDATE memories
                    SET
                        lifecycle_state = $1,
                        lifecycle_revision = lifecycle_revision + 1,
                        archived_at = $2,
                        archive_path = $3,
                        updated_at = $2
                    WHERE id = $4
                      AND lifecycle_revision = $5
                      AND lifecycle_state = $6
                    """,
                    EnumLifecycleState.ARCHIVED.value,
                    archived_at,
                    str(archive_path),
                    memory_id,
                    expected_revision,
                    EnumLifecycleState.EXPIRED.value,
                ),
                timeout=self._QUERY_TIMEOUT_SECONDS,
            )

            # Check if update affected any rows
            match = re.match(r"UPDATE (\d+)", result)
            if not match:
                logger.error(
                    "Unexpected execute result format",
                    extra={
                        "memory_id": str(memory_id),
                        "result": repr(result),
                    },
                )
                raise RuntimeError(f"Unexpected DB result format: {result}")
            rows_affected = int(match.group(1))
            return rows_affected > 0
