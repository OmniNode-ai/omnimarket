# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Runtime-profile ownership tests for delegation plugin consumers."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
import yaml
from omnibase_infra.enums import EnumDispatchStatus
from omnibase_infra.event_bus.topic_constants import (
    TOPIC_DELEGATION_COMPLETED,
    TOPIC_DELEGATION_TASK_DELEGATED,
)
from omnibase_infra.models.dispatch.model_dispatch_result import ModelDispatchResult
from omnibase_infra.runtime.event_bus_subcontract_wiring import (
    load_event_bus_subcontract,
    load_published_events_map,
)
from omnibase_infra.runtime.models import ModelDomainPluginConfig

from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_event import (
    ModelDelegationEvent,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_result import (
    ModelDelegationResult,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_task_delegated_event import (
    ModelTaskDelegatedEvent,
)
from omnimarket.nodes.node_delegation_orchestrator.plugin import (
    _CONTRACT_PATH,
    PluginDelegation,
    _build_delegation_result_applier,
)


class _RecordingEventBus:
    def __init__(self) -> None:
        self.published: list[tuple[str, object]] = []

    async def publish_envelope(
        self, *, envelope: object, topic: str, key: bytes | None = None
    ) -> None:
        self.published.append((topic, envelope))


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
def test_delegation_contract_declares_legacy_compatibility_publish_topic() -> None:
    contract = yaml.safe_load(_CONTRACT_PATH.read_text(encoding="utf-8"))

    assert contract["compatibility_publish_topics"] == [
        TOPIC_DELEGATION_TASK_DELEGATED,
    ]
    assert TOPIC_DELEGATION_TASK_DELEGATED in contract["event_bus"]["publish_topics"]


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
async def test_plugin_result_applier_allows_contract_terminal_topics() -> None:
    cid = uuid4()
    bus = _RecordingEventBus()
    subcontract = load_event_bus_subcontract(_CONTRACT_PATH)
    assert subcontract is not None

    applier = _build_delegation_result_applier(
        event_bus=bus,
        output_topic=TOPIC_DELEGATION_COMPLETED,
        published_events_map=load_published_events_map(_CONTRACT_PATH),
        publish_topics=subcontract.publish_topics,
    )
    terminal = ModelDelegationEvent(
        topic=TOPIC_DELEGATION_COMPLETED,
        payload=ModelDelegationResult(
            correlation_id=cid,
            task_type="test",
            model_used="local-coder",
            endpoint_url="http://127.0.0.1:8001",
            content="def test_example():\n    assert True",
            quality_passed=True,
            quality_score=1.0,
            latency_ms=42,
            fallback_to_claude=False,
        ),
    )
    compat = ModelTaskDelegatedEvent(
        topic=TOPIC_DELEGATION_TASK_DELEGATED,
        timestamp=datetime.now(UTC).isoformat(),
        correlation_id=cid,
        task_type="test",
        delegated_to="local-coder",
        model_name="local-coder",
        quality_gate_passed=True,
    )
    result = ModelDispatchResult(
        status=EnumDispatchStatus.SUCCESS,
        topic="onex.evt.omnibase-infra.quality-gate-result.v1",
        started_at=datetime.now(UTC),
        correlation_id=cid,
        output_events=[terminal, compat],
    )

    await applier.apply(result, cid)

    published_topics = [topic for topic, _envelope in bus.published]
    assert published_topics == [
        TOPIC_DELEGATION_COMPLETED,
        TOPIC_DELEGATION_TASK_DELEGATED,
    ]
    terminal_payload = bus.published[0][1].payload
    assert isinstance(terminal_payload, ModelDelegationResult)
    assert terminal_payload.content.startswith("def test_example")


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
