# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2025 OmniNode Team
"""Registry for intent_query_effect node dependency injection.

This registry provides factory methods for creating HandlerIntentQuery
instances with their required dependencies resolved.

Following ONEX naming conventions:
    - File: registry_<node_name>.py
    - Class: Registry<NodeName>

The registry serves as the entry point for creating properly configured
handler instances, documenting required adapters, and providing
node metadata for introspection.

Related:
    - models/: Configuration and mapping utilities
    - handlers/: Handler implementation

.. versionadded:: 0.1.0
    Initial implementation for OMN-1504.

.. versionchanged:: 0.2.0
    Refactored to container-driven pattern for OMN-1577.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from omnibase_core.container import ModelONEXContainer
    from omnimemory.handlers.adapters.models import ModelAdapterIntentGraphConfig

    from omnimarket.nodes.node_intent_query_effect.handlers import HandlerIntentQuery
    from omnimarket.nodes.node_intent_query_effect.models import (
        ModelHandlerIntentQueryConfig,
    )

__all__ = ["RegistryIntentQueryEffect"]


class RegistryIntentQueryEffect:
    """Infrastructure registry for intent_query_effect node.

    Provides factory methods for creating handler instances with
    proper dependency injection using the container-driven pattern.

    This registry follows the ONEX infrastructure registry pattern:
        - Factory methods for handler creation with container injection
        - Container-based dependency management
        - Node type classification for routing decisions
        - Capability listing for service discovery

    Example:
        >>> from omnibase_core.container import ModelONEXContainer
        >>> from omnimarket.nodes.node_intent_query_effect.registry import (
        ...     RegistryIntentQueryEffect,
        ... )
        >>>
        >>> # Create handler via registry with container
        >>> container = ModelONEXContainer()
        >>> handler = await RegistryIntentQueryEffect.create_and_initialize(
        ...     container=container,
        ...     connection_uri="bolt://{OMNIMEMORY_MEMGRAPH_HOST}:{OMNIMEMORY_MEMGRAPH_PORT}",
        ... )

    .. versionadded:: 0.1.0

    .. versionchanged:: 0.2.0
        Refactored to container-driven pattern for OMN-1577.
    """

    @staticmethod
    def create_handler(
        container: ModelONEXContainer,
    ) -> HandlerIntentQuery:
        """Create a HandlerIntentQuery with container.

        Factory method that creates a HandlerIntentQuery instance
        with the provided container. The handler must be initialized
        separately before use.

        Args:
            container: ONEX container for dependency injection.

        Returns:
            HandlerIntentQuery instance (not yet initialized).

        Example:
            >>> container = ModelONEXContainer()
            >>> handler = RegistryIntentQueryEffect.create_handler(container)
            >>> await handler.initialize(connection_uri="bolt://{OMNIMEMORY_MEMGRAPH_HOST}:{OMNIMEMORY_MEMGRAPH_PORT}")

        .. versionadded:: 0.1.0

        .. versionchanged:: 0.2.0
            Changed to accept container instead of config (OMN-1577).
        """
        from omnimarket.nodes.node_intent_query_effect.handlers import (
            HandlerIntentQuery,
        )

        return HandlerIntentQuery(container=container)

    @staticmethod
    async def create_and_initialize(
        container: ModelONEXContainer,
        connection_uri: str,
        auth: tuple[str, str] | None = None,
        *,
        config: ModelHandlerIntentQueryConfig | None = None,
        adapter_config: ModelAdapterIntentGraphConfig | None = None,
        options: Mapping[str, object] | None = None,
    ) -> HandlerIntentQuery:
        """Create and initialize a HandlerIntentQuery.

        Convenience method that creates and initializes in one call.
        The handler owns and manages the adapter lifecycle internally.

        Args:
            container: ONEX container for dependency injection.
            connection_uri: Graph database URI (e.g., "bolt://{OMNIMEMORY_MEMGRAPH_HOST}:{OMNIMEMORY_MEMGRAPH_PORT}").
            auth: Optional (username, password) tuple for authentication.
            config: Optional handler configuration. Uses defaults if not provided.
            adapter_config: Optional adapter configuration. Uses defaults if not provided.
            options: Additional connection options passed to the adapter.

        Returns:
            Initialized HandlerIntentQuery ready to execute queries.

        Example:
            >>> container = ModelONEXContainer()
            >>> handler = await RegistryIntentQueryEffect.create_and_initialize(
            ...     container=container,
            ...     connection_uri="bolt://{OMNIMEMORY_MEMGRAPH_HOST}:{OMNIMEMORY_MEMGRAPH_PORT}",
            ...     config=ModelHandlerIntentQueryConfig(timeout_seconds=30.0),
            ... )
            >>> response = await handler.execute(request)

        .. versionadded:: 0.1.0

        .. versionchanged:: 0.2.0
            Changed to container-driven pattern with handler-owned adapter (OMN-1577).
        """
        from omnimarket.nodes.node_intent_query_effect.handlers import (
            HandlerIntentQuery,
        )

        handler = HandlerIntentQuery(container=container)
        await handler.initialize(
            connection_uri=connection_uri,
            auth=auth,
            config=config,
            adapter_config=adapter_config,
            options=options,
        )
        return handler

    @staticmethod
    def get_required_adapters() -> list[str]:
        """Get list of adapters required by this node.

        Returns the adapter class names that must be provided when
        initializing handlers from this registry.

        Returns:
            List of adapter class names required for node operation.

        Example:
            >>> adapters = RegistryIntentQueryEffect.get_required_adapters()
            >>> print(adapters)
            ['AdapterIntentGraph']

        .. versionadded:: 0.1.0
        """
        return ["AdapterIntentGraph"]

    @staticmethod
    def get_node_type() -> str:
        """Get the ONEX node type classification.

        Returns the ONEX node archetype for this node, used for
        routing decisions and execution context selection.

        Returns:
            Node type string ("EFFECT").

        Note:
            EFFECT nodes perform external I/O operations and should
            be treated as side-effecting by the runtime.

        .. versionadded:: 0.1.0
        """
        return "EFFECT"

    @staticmethod
    def get_node_name() -> str:
        """Get the canonical node name.

        Returns:
            The node name identifier.

        .. versionadded:: 0.1.0
        """
        return "intent_query_effect"

    @staticmethod
    def get_capabilities() -> list[str]:
        """Get list of capabilities provided by this node.

        Returns capability identifiers that can be used for service
        discovery and feature detection.

        Returns:
            List of capability identifiers.

        .. versionadded:: 0.1.0
        """
        return [
            "intent_distribution_query",
            "intent_session_query",
            "intent_recent_query",
            "event_driven_response",
        ]

    @staticmethod
    def get_supported_query_types() -> list[str]:
        """Get list of supported query types.

        Returns the query type values that can be passed in
        intent query request events.

        Returns:
            List of supported query type strings.

        .. versionadded:: 0.1.0
        """
        return ["distribution", "session", "recent"]

    @staticmethod
    def get_invocation_mode() -> str:
        """Get the invocation mode for this node.

        Returns:
            "subscription" - node subscribes to topic, runtime dispatches
            "orchestrator" - node is invoked by orchestrator, no subscription

        .. versionadded:: 0.1.0
        """
        return "subscription"

    @staticmethod
    def get_supported_operations() -> list[str]:
        """Get list of operations supported by this node.

        Returns:
            List of operation identifiers.

        .. versionadded:: 0.1.0
        """
        return [
            "query_intent_distribution",
            "query_session_intents",
            "query_recent_intents",
        ]

    @staticmethod
    def get_backends() -> list[str]:
        """Get list of backend types this node interacts with.

        Returns:
            List of backend identifiers.

        .. versionadded:: 0.1.0
        """
        return ["memgraph", "kafka"]
