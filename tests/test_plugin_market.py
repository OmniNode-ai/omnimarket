# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for PluginMarket domain plugin."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from omnimarket.runtime.plugin import MARKET_SUBSCRIBE_TOPICS, PluginMarket

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def plugin() -> PluginMarket:
    return PluginMarket()


@pytest.fixture
def mock_config() -> MagicMock:
    config = MagicMock()
    config.correlation_id = "test-correlation-id"
    config.event_bus = MagicMock()
    config.event_bus.subscribe = AsyncMock(return_value=AsyncMock())
    config.container = MagicMock()
    return config


# ============================================================================
# Protocol compliance
# ============================================================================


def test_protocol_compliance() -> None:
    """PluginMarket satisfies ProtocolDomainPlugin at runtime."""
    from omnibase_infra.runtime.protocol_domain_plugin import ProtocolDomainPlugin

    plugin = PluginMarket()
    assert isinstance(plugin, ProtocolDomainPlugin)


# ============================================================================
# Properties
# ============================================================================


def test_plugin_id(plugin: PluginMarket) -> None:
    assert plugin.plugin_id == "market"


def test_display_name(plugin: PluginMarket) -> None:
    assert plugin.display_name == "Market"


# ============================================================================
# should_activate
# ============================================================================


def test_should_activate_with_kafka(
    plugin: PluginMarket, mock_config: MagicMock
) -> None:
    with patch.dict(os.environ, {"KAFKA_BOOTSTRAP_SERVERS": "localhost:9092"}):
        assert plugin.should_activate(mock_config) is True


def test_should_not_activate_without_kafka(
    plugin: PluginMarket, mock_config: MagicMock
) -> None:
    with patch.dict(os.environ, {}, clear=True):
        os.environ.pop("KAFKA_BOOTSTRAP_SERVERS", None)
        assert plugin.should_activate(mock_config) is False


# ============================================================================
# initialize
# ============================================================================


@pytest.mark.asyncio
async def test_initialize(plugin: PluginMarket, mock_config: MagicMock) -> None:
    result = await plugin.initialize(mock_config)
    assert result.success is True
    assert result.plugin_id == "market"


# ============================================================================
# wire_handlers
# ============================================================================


@pytest.mark.asyncio
async def test_wire_handlers(plugin: PluginMarket, mock_config: MagicMock) -> None:
    result = await plugin.wire_handlers(mock_config)
    assert result.success is True


# ============================================================================
# start_consumers
# ============================================================================


@pytest.mark.asyncio
async def test_start_consumers_no_event_bus(
    plugin: PluginMarket, mock_config: MagicMock
) -> None:
    mock_config.event_bus = object()  # no subscribe method
    result = await plugin.start_consumers(mock_config)
    assert not result.success or result.message  # skipped


@pytest.mark.asyncio
async def test_start_consumers_subscribes(
    plugin: PluginMarket, mock_config: MagicMock
) -> None:
    """Consumers subscribe to contract-declared topics when event bus supports it."""
    with patch.dict(os.environ, {"KAFKA_BOOTSTRAP_SERVERS": "localhost:9092"}):
        result = await plugin.start_consumers(mock_config)
        if MARKET_SUBSCRIBE_TOPICS:
            assert result.success is True
            assert mock_config.event_bus.subscribe.call_count == len(
                MARKET_SUBSCRIBE_TOPICS
            )


# ============================================================================
# shutdown
# ============================================================================


@pytest.mark.asyncio
async def test_shutdown_clean(plugin: PluginMarket, mock_config: MagicMock) -> None:
    result = await plugin.shutdown(mock_config)
    assert result.success is True


@pytest.mark.asyncio
async def test_shutdown_with_consumers(
    plugin: PluginMarket, mock_config: MagicMock
) -> None:
    """Shutdown unsubscribes from all topics."""
    unsub = AsyncMock()
    plugin._unsubscribe_callbacks = [unsub]
    result = await plugin.shutdown(mock_config)
    assert result.success is True
    unsub.assert_awaited_once()


@pytest.mark.asyncio
async def test_shutdown_idempotent(
    plugin: PluginMarket, mock_config: MagicMock
) -> None:
    """Concurrent shutdown calls are safe."""
    plugin._shutdown_in_progress = True
    result = await plugin.shutdown(mock_config)
    # Should skip rather than fail
    assert "already in progress" in (result.message or "").lower() or not result.success


# ============================================================================
# get_status_line
# ============================================================================


def test_status_line_disabled(plugin: PluginMarket) -> None:
    with patch.dict(os.environ, {}, clear=True):
        os.environ.pop("KAFKA_BOOTSTRAP_SERVERS", None)
        assert plugin.get_status_line() == "disabled"


def test_status_line_enabled(plugin: PluginMarket) -> None:
    with patch.dict(os.environ, {"KAFKA_BOOTSTRAP_SERVERS": "localhost:9092"}):
        line = plugin.get_status_line()
        assert line.startswith("enabled")


# ============================================================================
# Contract topic discovery
# ============================================================================


def test_subscribe_topics_discovered() -> None:
    """At least some subscribe topics are discovered from contracts."""
    assert len(MARKET_SUBSCRIBE_TOPICS) > 0


def test_subscribe_topics_are_strings() -> None:
    """All discovered topics are strings."""
    for topic in MARKET_SUBSCRIBE_TOPICS:
        assert isinstance(topic, str)
        assert topic.startswith("onex.")
