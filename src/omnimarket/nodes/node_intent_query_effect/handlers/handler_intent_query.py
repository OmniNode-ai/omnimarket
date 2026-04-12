# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2025 OmniNode Team
"""Handler for intent query operations via Kafka events.

Processes intent query requests (distribution, session, recent, health_check)
and returns responses via the event bus. Part of the event-driven architecture
where OmniDash queries intent data without direct database access.

This handler follows the container-driven pattern where the handler owns
the adapter lifecycle and manages all database connection setup internally.

Example::

    from omnibase_core.container import ModelONEXContainer
    from omnimarket.nodes.node_intent_query_effect.handlers import HandlerIntentQuery

    container = ModelONEXContainer()
    handler = HandlerIntentQuery(container)
    await handler.initialize(
        connection_uri="bolt://{OMNIMEMORY_MEMGRAPH_HOST}:{OMNIMEMORY_MEMGRAPH_PORT}",
        auth=("user", "password"),
    )
    response = await handler.execute(request_event)
    await handler.shutdown()

.. versionadded:: 0.1.0
    Initial implementation for OMN-1504.

.. versionchanged:: 0.2.0
    Refactored to container-driven pattern for OMN-1577.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from datetime import UTC
from typing import TYPE_CHECKING

from omnibase_core.models.events import ModelIntentRecordPayload
from omnimemory.handlers.adapters import AdapterIntentGraph
from omnimemory.handlers.adapters.models import ModelAdapterIntentGraphConfig
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_intent_query_effect.models import (
    ModelHandlerIntentQueryConfig,
    ModelIntentQueryRequestedEvent,
    ModelIntentQueryResponseEvent,
)
from omnimarket.nodes.node_intent_query_effect.utils import map_intent_records

if TYPE_CHECKING:
    from omnibase_core.container import ModelONEXContainer

logger = logging.getLogger(__name__)

# Contract SLA: max response time in milliseconds
_CONTRACT_MAX_RESPONSE_TIME_MS = 100.0

__all__ = ["HandlerIntentQuery", "ModelIntentQueryHealth", "ModelIntentQueryMetadata"]


class ModelIntentQueryConfigInfo(  # omnimemory-model-exempt: handler metadata
    BaseModel
):
    """Configuration info for intent query handler metadata.

    Attributes:
        timeout_seconds: Query timeout in seconds.
        default_time_range_hours: Default time range for queries.
        default_limit: Default result limit.
        default_min_confidence: Default minimum confidence threshold.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    timeout_seconds: float = Field(
        ...,
        description="Query timeout in seconds",
    )
    default_time_range_hours: int = Field(
        ...,
        description="Default time range for queries in hours",
    )
    default_limit: int = Field(
        ...,
        description="Default result limit",
    )
    default_min_confidence: float = Field(
        ...,
        description="Default minimum confidence threshold",
    )


class ModelIntentQueryHealth(  # omnimemory-model-exempt: handler health
    BaseModel
):
    """Health status for the Intent Query Handler.

    Returned by health_check() to provide detailed health information
    about the handler and its owned adapter.

    Attributes:
        healthy: Overall health status.
        initialized: Whether the handler has been initialized.
        adapter_healthy: Adapter health status.
        error_message: Error details if unhealthy.
        session_count: Number of sessions in the graph database.
        intent_count: Number of intents in the graph database.
    """

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=True,
    )

    healthy: bool = Field(
        ...,
        description="Overall health status",
    )
    initialized: bool = Field(
        ...,
        description="Whether the handler has been initialized",
    )
    adapter_healthy: bool | None = Field(
        default=None,
        description="Adapter health status",
    )
    error_message: str | None = Field(
        default=None,
        description="Error details if unhealthy",
    )
    session_count: int | None = Field(
        default=None,
        ge=0,
        description="Number of sessions in the graph database",
    )
    intent_count: int | None = Field(
        default=None,
        ge=0,
        description="Number of intents in the graph database",
    )


class ModelIntentQueryMetadata(  # omnimemory-model-exempt: handler metadata
    BaseModel
):
    """Metadata describing intent query handler capabilities and configuration.

    Returned by describe() method to provide introspection information
    about the handler's capabilities, supported query types, and configuration.

    Attributes:
        name: Handler class name.
        node_type: ONEX node type identifier.
        capabilities: List of supported operations.
        supported_query_types: List of supported query type values.
        initialized: Whether handler is ready.
        config: Current configuration (None if not initialized).
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    name: str = Field(
        ...,
        description="Handler class name",
    )
    node_type: str = Field(
        ...,
        description="ONEX node type identifier",
    )
    capabilities: list[str] = Field(
        ...,
        description="List of supported operations",
    )
    supported_query_types: list[str] = Field(
        ...,
        description="List of supported query type values",
    )
    initialized: bool = Field(
        ...,
        description="Whether handler is ready",
    )
    config: ModelIntentQueryConfigInfo | None = Field(
        default=None,
        description="Current configuration (None if not initialized)",
    )


class HandlerIntentQuery:
    """Handler for event-driven intent queries.

    Routes intent query requests to the appropriate adapter method and
    constructs response events with proper correlation tracking.

    This handler follows the container-driven pattern:
    - Constructor takes ModelONEXContainer for dependency injection
    - Handler owns and manages the adapter lifecycle
    - initialize() creates and configures the adapter internally

    Supported query types:
        - distribution: Get intent counts grouped by category
        - session: Get intents for a specific session
        - recent: Get recent intents across all sessions
        - health_check: Check handler health and readiness status

    Attributes:
        container: The ONEX dependency injection container.
        config: Handler configuration controlling timeouts and defaults.
        is_initialized: Whether the handler has been initialized.

    Example::

        import os
        from omnibase_core.container import ModelONEXContainer
        from omnimarket.nodes.node_intent_query_effect.handlers import HandlerIntentQuery

        # Create handler with container
        container = ModelONEXContainer()
        handler = HandlerIntentQuery(container)

        # Initialize (creates and owns adapter internally)
        await handler.initialize(
            connection_uri=os.getenv("MEMGRAPH_URI", "bolt://{OMNIMEMORY_MEMGRAPH_HOST}:{OMNIMEMORY_MEMGRAPH_PORT}"),
            auth=(os.getenv("MEMGRAPH_USER", ""), os.getenv("MEMGRAPH_PASSWORD", "")),
        )

        # Execute a query
        request = ModelIntentQueryRequestedEvent(
            query_type="distribution",
            time_range_hours=24,
        )
        response = await handler.execute(request)
        if response.status == "success":
            print(f"Distribution: {response.distribution}")

        # Shutdown releases all resources including adapter
        await handler.shutdown()

    .. versionchanged:: 0.2.0
        Refactored to container-driven pattern for OMN-1577.
    """

    def __init__(self, container: ModelONEXContainer) -> None:
        """Initialize the handler with a dependency injection container.

        Args:
            container: The ONEX container for dependency injection.
        """
        self._container = container
        self._config: ModelHandlerIntentQueryConfig | None = None
        self._adapter: AdapterIntentGraph | None = None
        self._initialized = False
        self._init_lock = asyncio.Lock()

    @property
    def container(self) -> ModelONEXContainer:
        """Get the dependency injection container."""
        return self._container

    @property
    def config(self) -> ModelHandlerIntentQueryConfig | None:
        """Get handler configuration (None if not initialized)."""
        return self._config

    @property
    def is_initialized(self) -> bool:
        """Check if handler is initialized."""
        return self._initialized

    async def initialize(
        self,
        connection_uri: str,
        auth: tuple[str, str] | None = None,
        *,
        config: ModelHandlerIntentQueryConfig | None = None,
        adapter_config: ModelAdapterIntentGraphConfig | None = None,
        options: Mapping[str, object] | None = None,
    ) -> None:
        """Initialize the handler and create owned adapter.

        This method creates and owns the adapter lifecycle internally.
        The adapter is created, initialized, and managed by this handler.

        Thread-safe initialization using asyncio.Lock.

        Args:
            connection_uri: Graph database URI (e.g., "bolt://{OMNIMEMORY_MEMGRAPH_HOST}:{OMNIMEMORY_MEMGRAPH_PORT}").
            auth: Optional (username, password) tuple for authentication.
            config: Optional handler configuration. Uses defaults if not provided.
            adapter_config: Optional adapter configuration. Uses defaults if not provided.
            options: Additional connection options passed to the adapter.

        Raises:
            RuntimeError: If initialization fails or times out.
            ValueError: If connection_uri is malformed.

        .. versionchanged:: 0.2.0
            Changed to create and own adapter internally (OMN-1577).
        """
        if self._initialized:
            return

        async with self._init_lock:
            if self._initialized:
                return

            # Set handler config
            self._config = config or ModelHandlerIntentQueryConfig()

            # Create and initialize adapter (handler owns lifecycle)
            effective_adapter_config = adapter_config or ModelAdapterIntentGraphConfig(
                timeout_seconds=self._config.timeout_seconds,
            )
            self._adapter = AdapterIntentGraph(
                config=effective_adapter_config,
                container=self._container,
            )
            await self._adapter.initialize(
                connection_uri=connection_uri,
                auth=auth,
                options=options,
            )

            self._initialized = True
            logger.info("HandlerIntentQuery initialized with owned adapter")

    async def health_check(self) -> ModelIntentQueryHealth:
        """Check the health status of this handler.

        Returns health information about the handler and its owned adapter.

        Returns:
            ModelIntentQueryHealth with detailed status information:
                - healthy: Overall health status
                - initialized: Whether handler is initialized
                - adapter_healthy: Adapter health status
                - error_message: Error details if unhealthy
                - session_count: Number of sessions in graph database
                - intent_count: Number of intents in graph database
        """
        if not self._initialized:
            return ModelIntentQueryHealth(
                healthy=False,
                initialized=False,
                adapter_healthy=None,
                error_message="Handler not initialized",
            )

        if self._adapter is None:
            return ModelIntentQueryHealth(
                healthy=False,
                initialized=True,
                adapter_healthy=None,
                error_message="Adapter is None despite initialization",
            )

        try:
            adapter_healthy = await self._adapter.health_check()
            return ModelIntentQueryHealth(
                healthy=adapter_healthy,
                initialized=True,
                adapter_healthy=adapter_healthy,
                error_message=None
                if adapter_healthy
                else "Adapter health check failed",
            )
        except Exception as e:
            logger.warning("Health check failed: %s", e)
            return ModelIntentQueryHealth(
                healthy=False,
                initialized=True,
                adapter_healthy=None,
                error_message=f"Health check failed: {e}",
            )

    async def describe(self) -> ModelIntentQueryMetadata:
        """Return handler metadata and capabilities.

        Provides introspection information about this handler including
        supported operations, configuration, and status.

        Returns:
            ModelIntentQueryMetadata with handler information including
            name, node_type, capabilities, supported query types, and configuration.
        """
        config_info: ModelIntentQueryConfigInfo | None = None
        if self._config is not None:
            config_info = ModelIntentQueryConfigInfo(
                timeout_seconds=self._config.timeout_seconds,
                default_time_range_hours=self._config.default_time_range_hours,
                default_limit=self._config.default_limit,
                default_min_confidence=self._config.default_min_confidence,
            )

        return ModelIntentQueryMetadata(
            name="HandlerIntentQuery",
            node_type="EFFECT",
            capabilities=[
                "intent_distribution_query",
                "intent_session_query",
                "intent_recent_query",
                "event_driven_response",
            ],
            supported_query_types=["distribution", "session", "recent"],
            initialized=self._initialized,
            config=config_info,
        )

    async def execute(
        self,
        request: ModelIntentQueryRequestedEvent,
    ) -> ModelIntentQueryResponseEvent:
        """Execute an intent query request.

        Routes the request to the appropriate handler method based on query_type.
        This method never raises - all errors are captured in the response.

        Args:
            request: The incoming query request event.

        Returns:
            Response event with query results or error details.
        """
        if not self._initialized or self._adapter is None or self._config is None:
            return ModelIntentQueryResponseEvent.from_error(
                query_id=request.query_id,
                query_type=request.query_type,
                error_message="Handler not initialized",
                correlation_id=request.correlation_id,
            )

        start = time.monotonic()
        config = self._config  # Local reference for type narrowing

        try:
            async with asyncio.timeout(config.timeout_seconds):
                match request.query_type:
                    case "distribution":
                        return await self._handle_distribution(request, start)
                    case "session":
                        return await self._handle_session(request, start)
                    case "recent":
                        return await self._handle_recent(request, start)
                    case "health_check":
                        return await self._handle_health_check(request, start)
                    case _:
                        return ModelIntentQueryResponseEvent.from_error(
                            query_id=request.query_id,
                            query_type=request.query_type,
                            error_message=f"Unknown query_type: {request.query_type}",
                            correlation_id=request.correlation_id,
                        )
        except TimeoutError:
            return ModelIntentQueryResponseEvent.from_error(
                query_id=request.query_id,
                query_type=request.query_type,
                error_message=f"Handler timeout after {config.timeout_seconds}s",
                correlation_id=request.correlation_id,
            )
        except Exception as e:
            logger.exception("Error executing intent query: %s", request.query_type)
            return ModelIntentQueryResponseEvent.from_error(
                query_id=request.query_id,
                query_type=request.query_type,
                error_message=str(e),
                correlation_id=request.correlation_id,
            )

    async def _handle_distribution(
        self,
        request: ModelIntentQueryRequestedEvent,
        start: float,
    ) -> ModelIntentQueryResponseEvent:
        """Handle distribution query - get intent counts by category.

        Args:
            request: The query request event.
            start: Start time for execution timing.

        Returns:
            Response event with distribution data or error.
        """
        assert self._adapter is not None

        result = await self._adapter.get_intent_distribution(
            time_range_hours=request.time_range_hours,
        )

        execution_time_ms = (time.monotonic() - start) * 1000

        if execution_time_ms > _CONTRACT_MAX_RESPONSE_TIME_MS:
            logger.warning(
                "Query %s exceeded SLA: %.2fms (max: %.2fms)",
                request.query_type,
                execution_time_ms,
                _CONTRACT_MAX_RESPONSE_TIME_MS,
            )

        if result.status == "error":
            return ModelIntentQueryResponseEvent.from_error(
                query_id=request.query_id,
                query_type="distribution",
                error_message=result.error_message or "Unknown error",
                correlation_id=request.correlation_id,
            )

        return ModelIntentQueryResponseEvent.create_distribution_response(
            query_id=request.query_id,
            distribution=dict(result.distribution),
            time_range_hours=request.time_range_hours,
            execution_time_ms=execution_time_ms,
            correlation_id=request.correlation_id,
        )

    async def _handle_session(
        self,
        request: ModelIntentQueryRequestedEvent,
        start: float,
    ) -> ModelIntentQueryResponseEvent:
        """Handle session query - get intents for a specific session.

        Args:
            request: The query request event.
            start: Start time for execution timing.

        Returns:
            Response event with session intents or error.
        """
        assert self._adapter is not None

        if not request.session_ref:
            return ModelIntentQueryResponseEvent.from_error(
                query_id=request.query_id,
                query_type="session",
                error_message="session_ref is required for session queries",
                correlation_id=request.correlation_id,
            )

        result = await self._adapter.get_session_intents(
            session_id=request.session_ref,
            min_confidence=request.min_confidence
            if request.min_confidence > 0
            else 0.0,
            limit=request.limit,
        )

        execution_time_ms = (time.monotonic() - start) * 1000

        if execution_time_ms > _CONTRACT_MAX_RESPONSE_TIME_MS:
            logger.warning(
                "Query %s exceeded SLA: %.2fms (max: %.2fms)",
                request.query_type,
                execution_time_ms,
                _CONTRACT_MAX_RESPONSE_TIME_MS,
            )

        if not result.success:
            return ModelIntentQueryResponseEvent.from_error(
                query_id=request.query_id,
                query_type="session",
                error_message=result.error_message or "Unknown error",
                correlation_id=request.correlation_id,
            )

        if not result.intents:
            return ModelIntentQueryResponseEvent.create_session_response(
                query_id=request.query_id,
                intents=[],
                execution_time_ms=execution_time_ms,
                correlation_id=request.correlation_id,
            )

        try:
            payloads: list[ModelIntentRecordPayload] = []
            for record in result.intents:
                created_at = record.created_at
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=UTC)
                payloads.append(
                    ModelIntentRecordPayload(
                        intent_id=record.intent_id,
                        session_ref=record.session_id,
                        intent_category=record.intent_category.value,
                        confidence=record.confidence,
                        keywords=list(record.keywords),
                        created_at=created_at,
                    )
                )
        except (ValueError, AttributeError) as e:
            logger.warning("Failed to map intent records: %s", e)
            return ModelIntentQueryResponseEvent.from_error(
                query_id=request.query_id,
                query_type="session",
                error_message=f"Failed to map intent records: {e}",
                correlation_id=request.correlation_id,
            )

        return ModelIntentQueryResponseEvent.create_session_response(
            query_id=request.query_id,
            intents=payloads,
            execution_time_ms=execution_time_ms,
            correlation_id=request.correlation_id,
        )

    async def _handle_recent(
        self,
        request: ModelIntentQueryRequestedEvent,
        start: float,
    ) -> ModelIntentQueryResponseEvent:
        """Handle recent query - get recent intents across all sessions.

        Args:
            request: The query request event.
            start: Start time for execution timing.

        Returns:
            Response event with recent intents or error.
        """
        assert self._adapter is not None

        result = await self._adapter.get_recent_intents(
            time_range_hours=request.time_range_hours,
            min_confidence=request.min_confidence
            if request.min_confidence > 0
            else None,
            limit=request.limit,
        )

        execution_time_ms = (time.monotonic() - start) * 1000

        if execution_time_ms > _CONTRACT_MAX_RESPONSE_TIME_MS:
            logger.warning(
                "Query %s exceeded SLA: %.2fms (max: %.2fms)",
                request.query_type,
                execution_time_ms,
                _CONTRACT_MAX_RESPONSE_TIME_MS,
            )

        if result.status == "error":
            return ModelIntentQueryResponseEvent.from_error(
                query_id=request.query_id,
                query_type="recent",
                error_message=result.error_message or "Unknown error",
                correlation_id=request.correlation_id,
            )

        if not result.intents:
            return ModelIntentQueryResponseEvent.create_recent_response(
                query_id=request.query_id,
                intents=[],
                time_range_hours=request.time_range_hours,
                execution_time_ms=execution_time_ms,
                correlation_id=request.correlation_id,
            )

        try:
            payloads = map_intent_records(result.intents)
        except ValueError as e:
            logger.warning("Failed to map intent records: %s", e)
            return ModelIntentQueryResponseEvent.from_error(
                query_id=request.query_id,
                query_type="recent",
                error_message=f"Failed to map intent records: {e}",
                correlation_id=request.correlation_id,
            )

        return ModelIntentQueryResponseEvent.create_recent_response(
            query_id=request.query_id,
            intents=payloads,
            time_range_hours=request.time_range_hours,
            execution_time_ms=execution_time_ms,
            correlation_id=request.correlation_id,
        )

    async def _handle_health_check(
        self,
        request: ModelIntentQueryRequestedEvent,
        start: float,
    ) -> ModelIntentQueryResponseEvent:
        """Handle health check query - verify handler is initialized and ready.

        Args:
            request: The query request event.
            start: Start time for execution timing.

        Returns:
            Response event with health status.
        """
        execution_time_ms = (time.monotonic() - start) * 1000

        # Handler is initialized if we reached here (checked in execute())
        return ModelIntentQueryResponseEvent(
            query_id=request.query_id,
            query_type="health_check",  # pyright: ignore[reportArgumentType]
            status="success",
            execution_time_ms=execution_time_ms,
            correlation_id=request.correlation_id,
        )

    async def shutdown(self) -> None:
        """Shutdown the handler and release all resources.

        This method properly closes the owned adapter and releases all
        associated resources. Safe to call multiple times.

        .. versionchanged:: 0.2.0
            Handler now owns adapter lifecycle and shuts it down (OMN-1577).
        """
        if self._initialized:
            # Shutdown owned adapter
            if self._adapter is not None:
                try:
                    await self._adapter.shutdown()
                except Exception as e:
                    logger.warning("Error shutting down adapter: %s", e)
                finally:
                    self._adapter = None

            self._config = None
            self._initialized = False
            logger.info("HandlerIntentQuery shutdown complete")
