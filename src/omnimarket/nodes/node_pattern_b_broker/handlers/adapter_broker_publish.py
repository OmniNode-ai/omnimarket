"""Publish adapter for Pattern B broker dispatch requests."""

from __future__ import annotations

from typing import Protocol

from omnimarket.nodes.node_pattern_b_broker.handlers.adapter_broker_acl import (
    AdapterPatternBBrokerAcl,
    ProtocolPatternBBrokerQualityGate,
)
from omnimarket.nodes.node_pattern_b_broker.handlers.adapter_broker_contract_config import (
    load_pattern_b_broker_config,
)
from omnimarket.nodes.node_pattern_b_broker.models import (
    EnumPatternBBrokerAclDecision,
    EnumPatternBBrokerState,
    ModelPatternBBrokerDispatchRequest,
    ModelPatternBBrokerPublishReceipt,
    ModelPatternBBrokerRuntimeConfig,
)


class ProtocolPatternBBrokerEventPublisher(Protocol):
    """Minimal publish surface required by the broker publish adapter."""

    async def publish(
        self,
        topic: str,
        key: bytes | None,
        value: bytes,
    ) -> None: ...


class AdapterPatternBBrokerPublish:
    """Publish typed Pattern B dispatch requests through the event bus."""

    def __init__(
        self,
        *,
        event_bus: ProtocolPatternBBrokerEventPublisher,
        config: ModelPatternBBrokerRuntimeConfig | None = None,
        quality_gate: ProtocolPatternBBrokerQualityGate | None = None,
    ) -> None:
        self._event_bus = event_bus
        self._config = config or load_pattern_b_broker_config()
        self._quality_gate = quality_gate or AdapterPatternBBrokerAcl(self._config)

    @property
    def config(self) -> ModelPatternBBrokerRuntimeConfig:
        return self._config

    async def publish(
        self,
        request: ModelPatternBBrokerDispatchRequest,
    ) -> ModelPatternBBrokerPublishReceipt:
        """Validate and publish a dispatch request to the contract topic."""
        self._enforce_quality_gate(request)
        payload = request.model_dump_json().encode("utf-8")
        key = str(request.request_id)
        topic = self._config.topics.dispatch_request_topic

        await self._event_bus.publish(
            topic=topic,
            key=key.encode("utf-8"),
            value=payload,
        )

        return ModelPatternBBrokerPublishReceipt(
            request_id=request.request_id,
            correlation_id=request.correlation_id,
            state=EnumPatternBBrokerState.published,
            topic=topic,
            key=key,
            payload_size_bytes=len(payload),
            wait_policy=request.wait_policy,
        )

    def _enforce_quality_gate(
        self, request: ModelPatternBBrokerDispatchRequest
    ) -> None:
        result = self._quality_gate.evaluate(request)
        if result.acl.decision is not EnumPatternBBrokerAclDecision.allow:
            raise PermissionError(result.acl.reason)


__all__ = ["AdapterPatternBBrokerPublish", "ProtocolPatternBBrokerEventPublisher"]
