"""Tests for Pattern B broker ACL and quality-gate behavior."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_pattern_b_broker.handlers.adapter_broker_acl import (
    AdapterPatternBBrokerAcl,
)
from omnimarket.nodes.node_pattern_b_broker.handlers.adapter_broker_contract_config import (
    load_pattern_b_broker_config,
)
from omnimarket.nodes.node_pattern_b_broker.handlers.adapter_broker_publish import (
    AdapterPatternBBrokerPublish,
)
from omnimarket.nodes.node_pattern_b_broker.models import (
    EnumPatternBBrokerAclDecision,
    EnumPatternBBrokerOriginator,
    EnumPatternBBrokerRecipient,
    ModelPatternBBrokerAclResult,
    ModelPatternBBrokerDispatchRequest,
    ModelPatternBBrokerQualityGateResult,
    ModelPatternBBrokerRuntimeConfig,
)

_BROKER_CONTRACT = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_pattern_b_broker"
    / "contract.yaml"
)
_ACL_ADAPTER = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_pattern_b_broker"
    / "handlers"
    / "adapter_broker_acl.py"
)


@pytest.mark.unit
def test_acl_allows_contract_approved_request() -> None:
    request = _make_request()

    result = AdapterPatternBBrokerAcl(
        load_pattern_b_broker_config(_BROKER_CONTRACT)
    ).evaluate(request)

    assert result.allowed is True
    assert result.acl.decision is EnumPatternBBrokerAclDecision.allow
    assert result.request_id == request.request_id
    assert result.correlation_id == request.correlation_id


@pytest.mark.unit
def test_acl_denies_originator_not_allowed_by_config() -> None:
    default_config = load_pattern_b_broker_config(_BROKER_CONTRACT)
    config = ModelPatternBBrokerRuntimeConfig(
        topics=default_config.topics,
        consumer_group=default_config.consumer_group,
        default_wait_policy=default_config.default_wait_policy,
        allowed_originators=(EnumPatternBBrokerOriginator.omnicodex,),
        allowed_recipients=default_config.allowed_recipients,
    )
    request = _make_request(originator=EnumPatternBBrokerOriginator.omnimarket)

    result = AdapterPatternBBrokerAcl(config).evaluate(request)

    assert result.allowed is False
    assert result.acl.decision is EnumPatternBBrokerAclDecision.deny
    assert result.acl.matched_rule == "originator-allowlist"


@pytest.mark.unit
def test_acl_denies_recipient_not_allowed_by_config() -> None:
    default_config = load_pattern_b_broker_config(_BROKER_CONTRACT)
    config = ModelPatternBBrokerRuntimeConfig(
        topics=default_config.topics,
        consumer_group=default_config.consumer_group,
        default_wait_policy=default_config.default_wait_policy,
        allowed_originators=default_config.allowed_originators,
        allowed_recipients=(EnumPatternBBrokerRecipient.omnicodex,),
    )
    request = _make_request(recipient=EnumPatternBBrokerRecipient.omniclaude)

    result = AdapterPatternBBrokerAcl(config).evaluate(request)

    assert result.allowed is False
    assert result.acl.decision is EnumPatternBBrokerAclDecision.deny
    assert result.acl.matched_rule == "recipient-allowlist"


@pytest.mark.asyncio
async def test_publish_adapter_requires_explicit_quality_gate_allow() -> None:
    bus = EventBusInmemory()
    await bus.start()
    try:
        config = load_pattern_b_broker_config(_BROKER_CONTRACT)
        request = _make_request()
        adapter = AdapterPatternBBrokerPublish(
            event_bus=bus,
            config=config,
            quality_gate=_AlwaysDenyGate(),
        )

        with pytest.raises(PermissionError, match="blocked by fixture gate"):
            await adapter.publish(request)

        history = await bus.get_event_history(
            topic=config.topics.dispatch_request_topic
        )
        assert history == []
    finally:
        await bus.close()


@pytest.mark.unit
def test_acl_adapter_has_no_topic_literals_or_client_names() -> None:
    source = _ACL_ADAPTER.read_text(encoding="utf-8")

    assert "onex.cmd." not in source
    assert "onex.evt." not in source
    assert "PatternBrokerClient" not in source


class _AlwaysDenyGate:
    def evaluate(
        self,
        request: ModelPatternBBrokerDispatchRequest,
    ) -> ModelPatternBBrokerQualityGateResult:
        return ModelPatternBBrokerQualityGateResult(
            request_id=request.request_id,
            correlation_id=request.correlation_id,
            acl=ModelPatternBBrokerAclResult(
                decision=EnumPatternBBrokerAclDecision.deny,
                reason="blocked by fixture gate",
                matched_rule="fixture-deny",
            ),
        )


def _make_request(
    *,
    originator: EnumPatternBBrokerOriginator = EnumPatternBBrokerOriginator.omnimarket,
    recipient: EnumPatternBBrokerRecipient = EnumPatternBBrokerRecipient.omniclaude,
) -> ModelPatternBBrokerDispatchRequest:
    return ModelPatternBBrokerDispatchRequest(
        correlation_id=uuid4(),
        originator=originator,
        recipient=recipient,
        skill_name="session-orchestrator",
    )
