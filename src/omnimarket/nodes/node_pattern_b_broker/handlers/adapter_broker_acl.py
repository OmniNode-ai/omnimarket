"""ACL and quality-gate adapter for Pattern B broker publication."""

from __future__ import annotations

from typing import Protocol

from omnimarket.nodes.node_pattern_b_broker.handlers.adapter_broker_contract_config import (
    load_pattern_b_broker_config,
)
from omnimarket.nodes.node_pattern_b_broker.models import (
    EnumPatternBBrokerAclDecision,
    EnumPatternBBrokerState,
    ModelPatternBBrokerAclResult,
    ModelPatternBBrokerDispatchRequest,
    ModelPatternBBrokerQualityGateResult,
    ModelPatternBBrokerRuntimeConfig,
)


class ProtocolPatternBBrokerQualityGate(Protocol):
    """Publish-time gate interface for brokered dispatch requests."""

    def evaluate(
        self,
        request: ModelPatternBBrokerDispatchRequest,
    ) -> ModelPatternBBrokerQualityGateResult: ...


class AdapterPatternBBrokerAcl:
    """Evaluate the minimum Pattern B broker ACL before publication."""

    def __init__(self, config: ModelPatternBBrokerRuntimeConfig | None = None) -> None:
        self._config = config or load_pattern_b_broker_config()

    @property
    def config(self) -> ModelPatternBBrokerRuntimeConfig:
        return self._config

    def evaluate(
        self,
        request: ModelPatternBBrokerDispatchRequest,
    ) -> ModelPatternBBrokerQualityGateResult:
        acl = self._evaluate_acl(request)
        return ModelPatternBBrokerQualityGateResult(
            acl=acl,
            request_id=request.request_id,
            correlation_id=request.correlation_id,
        )

    def _evaluate_acl(
        self,
        request: ModelPatternBBrokerDispatchRequest,
    ) -> ModelPatternBBrokerAclResult:
        if request.state is not EnumPatternBBrokerState.accepted:
            return _deny("request state must be accepted", "state-accepted")
        if request.originator not in self._config.allowed_originators:
            return _deny(
                f"originator {request.originator.value!r} is not allowed",
                "originator-allowlist",
            )
        if request.recipient not in self._config.allowed_recipients:
            return _deny(
                f"recipient {request.recipient.value!r} is not allowed",
                "recipient-allowlist",
            )
        return ModelPatternBBrokerAclResult(
            decision=EnumPatternBBrokerAclDecision.allow,
            reason="originator, recipient, and request state are broker-allowed",
            matched_rule="broker-minimum-allowlist",
        )


def _deny(reason: str, matched_rule: str) -> ModelPatternBBrokerAclResult:
    return ModelPatternBBrokerAclResult(
        decision=EnumPatternBBrokerAclDecision.deny,
        reason=reason,
        matched_rule=matched_rule,
    )


__all__ = ["AdapterPatternBBrokerAcl", "ProtocolPatternBBrokerQualityGate"]
