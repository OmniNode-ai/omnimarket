# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""PluginMarket — domain plugin for OmniMarket kernel integration.

Implements ProtocolDomainPlugin so that omnimarket's lifecycle can be
managed by the kernel's generic plugin loader via the ``onex.domain_plugins``
entry point.

The plugin handles:
    - Contract-driven topic discovery from omnimarket node contracts
    - Kafka topic subscriptions for omnimarket events
    - Graceful shutdown and resource cleanup

Configuration:
    The plugin activates based on environment variables:
    - KAFKA_BOOTSTRAP_SERVERS: Required for plugin activation

Related:
    - OMN-7645: Create PluginMarket domain plugin
    - omnimemory/runtime/plugin.py (reference implementation)
"""

from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from omnibase_core.runtime.runtime_message_dispatch import MessageDispatchEngine

from omnibase_infra.runtime.protocol_domain_plugin import (
    ModelDomainPluginConfig,
    ModelDomainPluginResult,
    ProtocolDomainPlugin,
)

from omnimarket.runtime.contract_topics import (
    canonical_topic_to_dispatch_alias,
    collect_subscribe_topics_from_contracts,
)

logger = logging.getLogger(__name__)

_PLUGIN_ID = "market"
_DISPLAY_NAME = "Market"

# Consumer group for all omnimarket Kafka consumers.
_MARKET_CONSUMER_GROUP_DEFAULT = "omnimarket-handlers"

# ============================================================================
# Topic collection (import-time, contract-driven)
# ============================================================================

try:
    MARKET_SUBSCRIBE_TOPICS: list[str] = collect_subscribe_topics_from_contracts()
except Exception:
    logger.error(
        "Failed to collect subscribe topics from contracts — "
        "plugin will not receive events",
        exc_info=True,
    )
    MARKET_SUBSCRIBE_TOPICS: list[str] = []  # type: ignore[no-redef]


class PluginMarket:
    """OmniMarket domain plugin for ONEX kernel initialization.

    Provides omnimarket event-driven pipeline integration:
    - Contract-driven topic discovery
    - Kafka subscription management
    - Graceful shutdown

    Thread Safety:
        This class is NOT thread-safe. The kernel calls plugin methods
        sequentially during bootstrap.
    """

    def __init__(self) -> None:
        self._unsubscribe_callbacks: list[Callable[[], Awaitable[None]]] = []
        self._shutdown_in_progress: bool = False
        self._dispatch_engine: MessageDispatchEngine | None = None

    @property
    def plugin_id(self) -> str:
        return _PLUGIN_ID

    @property
    def display_name(self) -> str:
        return _DISPLAY_NAME

    def should_activate(self, config: ModelDomainPluginConfig) -> bool:
        """Activate when Kafka is configured."""
        kafka = os.getenv("KAFKA_BOOTSTRAP_SERVERS")
        if not kafka:
            logger.debug(
                "Market plugin inactive: KAFKA_BOOTSTRAP_SERVERS not set "
                "(correlation_id=%s)",
                config.correlation_id,
            )
            return False
        return True

    async def initialize(
        self,
        config: ModelDomainPluginConfig,
    ) -> ModelDomainPluginResult:
        """Initialize market resources.

        The market domain does not require a dedicated database pool at
        plugin level. This method validates the environment and prepares
        for consumer wiring.
        """
        return ModelDomainPluginResult(
            plugin_id=self.plugin_id,
            success=True,
            message="Market plugin initialized",
            resources_created=["contract_topic_discovery"],
        )

    async def wire_handlers(
        self,
        config: ModelDomainPluginConfig,
    ) -> ModelDomainPluginResult:
        """No-op — market handlers are wired at dispatch time."""
        return ModelDomainPluginResult(
            plugin_id=self.plugin_id,
            success=True,
            message="Market handlers wired (no-op, dispatch-only)",
        )

    async def wire_dispatchers(
        self,
        config: ModelDomainPluginConfig,
    ) -> ModelDomainPluginResult:
        """Wire market dispatchers (placeholder for future dispatch engine)."""
        return ModelDomainPluginResult.skipped(
            plugin_id=self.plugin_id,
            reason="Dispatch engine wiring deferred — market nodes use standalone execution",
        )

    async def start_consumers(
        self,
        config: ModelDomainPluginConfig,
    ) -> ModelDomainPluginResult:
        """Start market event consumers.

        Subscribes to all contract-declared topics. Messages are forwarded
        to handlers via the dispatch engine when available, or logged as
        unrouted when no dispatch engine is wired.
        """
        correlation_id = config.correlation_id

        if not hasattr(config.event_bus, "subscribe"):
            return ModelDomainPluginResult.skipped(
                plugin_id=self.plugin_id,
                reason="Event bus does not support subscribe",
            )

        if not MARKET_SUBSCRIBE_TOPICS:
            return ModelDomainPluginResult.skipped(
                plugin_id=self.plugin_id,
                reason="No subscribe topics discovered from contracts",
            )

        consumer_group = os.getenv(
            "OMNIMARKET_CONSUMER_GROUP",
            _MARKET_CONSUMER_GROUP_DEFAULT,
        )

        try:
            unsubscribe_callbacks: list[Callable[[], Awaitable[None]]] = []

            for topic in MARKET_SUBSCRIBE_TOPICS:
                logger.info(
                    "Subscribing to market topic: %s (correlation_id=%s)",
                    topic,
                    correlation_id,
                )
                unsub = await config.event_bus.subscribe(
                    topic=topic,
                    group_id=consumer_group,
                    on_message=self._make_topic_handler(topic, correlation_id),
                )
                unsubscribe_callbacks.append(unsub)

            self._unsubscribe_callbacks = unsubscribe_callbacks

            return ModelDomainPluginResult(
                plugin_id=self.plugin_id,
                success=True,
                message=(
                    f"Market consumers started ({len(unsubscribe_callbacks)} topics)"
                ),
                unsubscribe_callbacks=unsubscribe_callbacks,
            )

        except Exception as e:
            logger.exception(
                "Failed to start market consumers (correlation_id=%s)",
                correlation_id,
            )
            return ModelDomainPluginResult.failed(
                plugin_id=self.plugin_id,
                error_message=str(e),
            )

    async def shutdown(
        self,
        config: ModelDomainPluginConfig,
    ) -> ModelDomainPluginResult:
        """Clean up market resources."""
        if self._shutdown_in_progress:
            return ModelDomainPluginResult.skipped(
                plugin_id=self.plugin_id,
                reason="Shutdown already in progress",
            )
        self._shutdown_in_progress = True

        try:
            errors: list[str] = []

            for unsub in self._unsubscribe_callbacks:
                try:
                    await unsub()
                except Exception as unsub_error:
                    errors.append(f"unsubscribe: {unsub_error}")
                    logger.warning(
                        "Failed to unsubscribe market consumer: %s",
                        unsub_error,
                    )
            self._unsubscribe_callbacks = []
            self._dispatch_engine = None

            if errors:
                return ModelDomainPluginResult.failed(
                    plugin_id=self.plugin_id,
                    error_message="; ".join(errors),
                )

            return ModelDomainPluginResult.succeeded(
                plugin_id=self.plugin_id,
                message="Market resources cleaned up",
            )
        finally:
            self._shutdown_in_progress = False

    def get_status_line(self) -> str:
        """Get status line for kernel banner."""
        kafka = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "")
        if not kafka:
            return "disabled"
        topics_count = len(MARKET_SUBSCRIBE_TOPICS)
        return f"enabled ({topics_count} topics)"

    def _make_topic_handler(
        self,
        topic: str,
        correlation_id: object,
    ) -> Callable[[object], Awaitable[None]]:
        """Create an async handler for a topic subscription."""
        dispatch_alias = canonical_topic_to_dispatch_alias(topic)

        async def _handler(message: object) -> None:
            if self._dispatch_engine is not None:
                await self._dispatch_engine.dispatch(dispatch_alias, message)
            else:
                logger.debug(
                    "Market message received on %s but no dispatch engine wired "
                    "(correlation_id=%s)",
                    topic,
                    correlation_id,
                )

        return _handler


# Verify protocol compliance at module load time
_: ProtocolDomainPlugin = PluginMarket()

__all__: list[str] = [
    "MARKET_SUBSCRIBE_TOPICS",
    "PluginMarket",
]
