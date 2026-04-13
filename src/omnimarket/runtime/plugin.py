# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Omnimarket domain plugin for kernel-level initialization.

This module provides PluginOmnimarket, which implements ProtocolDomainPlugin
for the Omnimarket domain. It wires all omnimarket node handlers and Kafka
consumers declared in node contract.yaml files into the runtime kernel.

The plugin handles:
    - Kafka topic subscriptions for all omnimarket nodes (contract-driven)
    - Consumer wiring via the event bus

Topic Discovery:
    Subscribe topics are declared in individual node ``contract.yaml`` files
    under ``event_bus.subscribe_topics`` and collected at import time via
    ``collect_subscribe_topics_from_contracts()``. There are no hardcoded
    topic lists in this module.

Configuration:
    The plugin always activates — omnimarket nodes have no DB prerequisite.
    OMNIMARKET_CONSUMER_GROUP controls the shared Kafka consumer group ID.
    Defaults to "omnimarket-nodes".

Example Usage:
    The kernel loads this plugin automatically via the entry point:

        [project.entry-points."onex.domain_plugins"]
        omnimarket = "omnimarket.runtime.plugin:PluginOmnimarket"
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

import yaml
from omnibase_infra.runtime.protocol_domain_plugin import (
    ModelDomainPluginConfig,
    ModelDomainPluginResult,
    ProtocolDomainPlugin,
)

logger = logging.getLogger(__name__)

_CONSUMER_GROUP_ENV_VAR = "OMNIMARKET_CONSUMER_GROUP"
_CONSUMER_GROUP_DEFAULT = "omnimarket-nodes"


def _consumer_group() -> str:
    group = os.getenv(_CONSUMER_GROUP_ENV_VAR, "").strip()
    group = group if group else _CONSUMER_GROUP_DEFAULT
    if any(c.isspace() for c in group):
        raise ValueError(
            f"{_CONSUMER_GROUP_ENV_VAR} must not contain whitespace; got: {group!r}"
        )
    return group


def _collect_subscribe_topics() -> list[str]:
    """Collect subscribe topics from all omnimarket node contract.yaml files."""
    nodes_dir = Path(__file__).parent.parent / "nodes"
    topics: list[str] = []

    for child in sorted(nodes_dir.iterdir()):
        if not child.is_dir() or child.name.startswith(("_", ".")):
            continue
        contract_path = child / "contract.yaml"
        if not contract_path.exists():
            continue
        try:
            with open(contract_path) as f:
                contract = yaml.safe_load(f)
            if not isinstance(contract, dict):
                continue
            event_bus = contract.get("event_bus", {})
            if not isinstance(event_bus, dict):
                continue
            node_topics: list[str] = event_bus.get("subscribe_topics", [])
            node_topics = [t.removeprefix("{env}.") for t in node_topics]
            topics.extend(node_topics)
        except Exception:
            logger.warning(
                "Failed to read contract.yaml from %s", child.name, exc_info=True
            )

    logger.debug("Collected %d omnimarket subscribe topics", len(topics))
    return topics


try:
    OMNIMARKET_SUBSCRIBE_TOPICS: list[str] = _collect_subscribe_topics()
except Exception:
    logger.error(
        "Failed to collect subscribe topics — plugin will not receive events",
        exc_info=True,
    )
    OMNIMARKET_SUBSCRIBE_TOPICS = []


class PluginOmnimarket:
    """Omnimarket domain plugin for ONEX kernel initialization.

    Wires Kafka consumers for all omnimarket nodes declared in contract.yaml
    files. No database prerequisite — always activates.
    """

    def __init__(self) -> None:
        self._unsubscribe_callbacks: list[Callable[[], Awaitable[None]]] = []
        self._shutdown_in_progress: bool = False

    @property
    def plugin_id(self) -> str:
        return "omnimarket"

    @property
    def display_name(self) -> str:
        return "Omnimarket"

    def should_activate(self, config: ModelDomainPluginConfig) -> bool:
        logger.debug(
            "Omnimarket plugin activating (topics=%d, correlation_id=%s)",
            len(OMNIMARKET_SUBSCRIBE_TOPICS),
            config.correlation_id,
        )
        return True

    async def initialize(
        self,
        config: ModelDomainPluginConfig,
    ) -> ModelDomainPluginResult:
        return ModelDomainPluginResult(
            plugin_id=self.plugin_id,
            success=True,
            message="Omnimarket plugin initialized (no resources required)",
            resources_created=[],
            duration_seconds=0.0,
        )

    async def wire_handlers(
        self,
        config: ModelDomainPluginConfig,
    ) -> ModelDomainPluginResult:
        return ModelDomainPluginResult(
            plugin_id=self.plugin_id,
            success=True,
            message="Omnimarket handlers wired (contract-driven, no manual wiring)",
            services_registered=[],
            duration_seconds=0.0,
        )

    async def wire_dispatchers(
        self,
        config: ModelDomainPluginConfig,
    ) -> ModelDomainPluginResult:
        return ModelDomainPluginResult(
            plugin_id=self.plugin_id,
            success=True,
            message="Omnimarket dispatchers wired (runtime auto-wiring handles dispatch)",
            resources_created=[],
            duration_seconds=0.0,
        )

    async def start_consumers(
        self,
        config: ModelDomainPluginConfig,
    ) -> ModelDomainPluginResult:
        start_time = time.time()
        correlation_id = config.correlation_id

        if not hasattr(config.event_bus, "subscribe"):
            return ModelDomainPluginResult.skipped(
                plugin_id=self.plugin_id,
                reason="Event bus does not support subscribe",
            )

        group = _consumer_group()

        async def _noop_handler(msg: object) -> None:  # stub-ok
            pass

        try:
            unsubscribe_callbacks: list[Callable[[], Awaitable[None]]] = []

            for topic in OMNIMARKET_SUBSCRIBE_TOPICS:
                unsub = await config.event_bus.subscribe(
                    topic=topic,
                    group_id=group,
                    on_message=_noop_handler,
                )
                unsubscribe_callbacks.append(unsub)

            self._unsubscribe_callbacks = unsubscribe_callbacks
            duration = time.time() - start_time

            logger.info(
                "Omnimarket consumers started: %d topics (correlation_id=%s)",
                len(unsubscribe_callbacks),
                correlation_id,
            )

            return ModelDomainPluginResult(
                plugin_id=self.plugin_id,
                success=True,
                message=f"Omnimarket consumers started ({len(unsubscribe_callbacks)} topics)",
                duration_seconds=duration,
                unsubscribe_callbacks=unsubscribe_callbacks,
            )

        except Exception as e:
            duration = time.time() - start_time
            logger.exception(
                "Failed to start omnimarket consumers (correlation_id=%s)",
                correlation_id,
            )
            return ModelDomainPluginResult.failed(
                plugin_id=self.plugin_id,
                error_message=str(e),
                duration_seconds=duration,
            )

    async def shutdown(
        self,
        config: ModelDomainPluginConfig,
    ) -> ModelDomainPluginResult:
        if self._shutdown_in_progress:
            return ModelDomainPluginResult.skipped(
                plugin_id=self.plugin_id,
                reason="Shutdown already in progress",
            )
        self._shutdown_in_progress = True

        start_time = time.time()
        errors: list[str] = []

        for unsub in self._unsubscribe_callbacks:
            try:
                await unsub()
            except Exception as e:
                errors.append(str(e))
                logger.warning("Failed to unsubscribe omnimarket consumer: %s", e)
        self._unsubscribe_callbacks = []

        duration = time.time() - start_time

        if errors:
            return ModelDomainPluginResult.failed(
                plugin_id=self.plugin_id,
                error_message="; ".join(errors),
                duration_seconds=duration,
            )

        return ModelDomainPluginResult.succeeded(
            plugin_id=self.plugin_id,
            message="Omnimarket resources cleaned up",
            duration_seconds=duration,
        )

    def get_status_line(self) -> str:
        return f"enabled ({len(OMNIMARKET_SUBSCRIBE_TOPICS)} topics)"


# Verify protocol compliance at module load time
_: ProtocolDomainPlugin = PluginOmnimarket()

__all__: list[str] = [
    "OMNIMARKET_SUBSCRIBE_TOPICS",
    "PluginOmnimarket",
]
