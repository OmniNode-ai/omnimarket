# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Runtime-profile ownership tests for delegation plugin consumers."""

from __future__ import annotations

from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from omnibase_infra.runtime.models import ModelDomainPluginConfig

from omnimarket.nodes.node_delegation_orchestrator.plugin import PluginDelegation


def _plugin_config(runtime_profile: str = "main") -> ModelDomainPluginConfig:
    return ModelDomainPluginConfig(
        container=MagicMock(),
        event_bus=MagicMock(),
        correlation_id=uuid4(),
        input_topic="onex.cmd.omnibase-infra.delegation-request.v1",
        output_topic="onex.evt.omnibase-infra.delegation-completed.v1",
        consumer_group="local.runtime_config.delegation-orchestrator.consume.1.0.0",
        dispatch_engine=None,
        runtime_profile=runtime_profile,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dispatcher_routes_are_contract_managed() -> None:
    plugin = PluginDelegation()
    config = _plugin_config()
    config.dispatch_engine = MagicMock()

    result = await plugin.wire_dispatchers(config)

    assert result.success
    assert result.message == "Delegation dispatcher routes are contract-managed"
    assert plugin._dispatcher_wiring_succeeded is True
    config.dispatch_engine.register_dispatcher.assert_not_called()
    config.dispatch_engine.register_route.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_effects_profile_does_not_start_delegation_orchestration_consumers() -> (
    None
):
    plugin = PluginDelegation()
    plugin._handler_wiring_succeeded = True
    plugin._dispatcher_wiring_succeeded = True

    result = await plugin.start_consumers(_plugin_config(runtime_profile="effects"))

    assert result.success
    assert "runtime profile does not own delegation orchestration consumers" in (
        result.message
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_main_profile_keeps_delegation_orchestration_consumer_ownership() -> None:
    plugin = PluginDelegation()
    plugin._handler_wiring_succeeded = True
    plugin._dispatcher_wiring_succeeded = True

    result = await plugin.start_consumers(_plugin_config(runtime_profile="main"))

    assert result.success
    assert "dispatch_engine not available" in result.message


@pytest.mark.unit
@pytest.mark.asyncio
async def test_legacy_default_profile_keeps_delegation_orchestration_ownership() -> (
    None
):
    plugin = PluginDelegation()
    plugin._handler_wiring_succeeded = True
    plugin._dispatcher_wiring_succeeded = True

    result = await plugin.start_consumers(_plugin_config(runtime_profile="default"))

    assert result.success
    assert "dispatch_engine not available" in result.message
