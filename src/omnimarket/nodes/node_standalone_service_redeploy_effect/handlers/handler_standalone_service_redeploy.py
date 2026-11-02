# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Publish a scoped standalone-service recreate command to the deploy agent.

The contract declares the lanes the route serves and the compose files and services it may
touch. A request outside either is refused before anything is signed or published. The command
goes over the bus, signed with the deploy agent's shared secret, to the topic the contract
declares; the agent recreates that one service and answers on its own result topic. This
handler never runs docker and never waits for the agent.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
from typing import Any

import yaml

from omnimarket.nodes.node_standalone_service_redeploy_effect.models.model_standalone_service_redeploy import (
    EnumStandaloneRedeployOutcome,
    EnumStandaloneRedeployRefusal,
    ModelStandaloneRedeployTarget,
    ModelStandaloneServiceRedeployRequest,
    ModelStandaloneServiceRedeployResult,
)

_CONTRACT = Path(__file__).resolve().parent.parent / "contract.yaml"
_SECRET_ENV = "DEPLOY_AGENT_HMAC_SECRET"  # env var name, not a value


def _load_contract() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(_CONTRACT.read_text())
    return loaded


def load_declared_targets() -> tuple[ModelStandaloneRedeployTarget, ...]:
    """The compose-file and service pairs the contract declares."""
    return tuple(
        ModelStandaloneRedeployTarget.model_validate(item)
        for item in _load_contract()["standalone_targets"]
    )


def load_allowed_lanes() -> frozenset[str]:
    """The runtime lanes the contract serves."""
    return frozenset(str(lane) for lane in _load_contract()["allowed_lanes"])


def load_command_topic() -> str:
    """The one topic the contract publishes the recreate command to."""
    (topic,) = _load_contract()["event_bus"]["publish_topics"]
    return str(topic)


def _sign(payload: dict[str, Any], secret: str) -> dict[str, Any]:
    body = {k: v for k, v in payload.items() if k != "_signature"}
    raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    return {
        **body,
        "_signature": hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest(),
    }


class HandlerStandaloneServiceRedeploy:
    """Validate a request against the contract, then publish it signed."""

    def __init__(self, event_bus: Any) -> None:
        self._bus: Any = event_bus
        self._targets = load_declared_targets()
        self._lanes = load_allowed_lanes()
        self._topic = load_command_topic()

    def _refusal(
        self, request: ModelStandaloneServiceRedeployRequest
    ) -> tuple[EnumStandaloneRedeployRefusal, str] | None:
        if request.runtime_lane not in self._lanes:
            return (
                EnumStandaloneRedeployRefusal.LANE_NOT_ALLOWED,
                f"runtime lane {request.runtime_lane!r} is not served by this route",
            )
        files = {t.compose_file for t in self._targets}
        if request.compose_file not in files:
            return (
                EnumStandaloneRedeployRefusal.COMPOSE_FILE_NOT_DECLARED,
                f"compose file {request.compose_file!r} is not declared in the contract",
            )
        if not any(
            t.compose_file == request.compose_file and t.service == request.service
            for t in self._targets
        ):
            return (
                EnumStandaloneRedeployRefusal.SERVICE_NOT_DECLARED,
                f"service {request.service!r} is not declared for "
                f"{request.compose_file!r} in the contract",
            )
        return None

    async def handle(
        self, request: ModelStandaloneServiceRedeployRequest
    ) -> ModelStandaloneServiceRedeployResult:
        refused = self._refusal(request)
        if refused is not None:
            reason, message = refused
            return ModelStandaloneServiceRedeployResult(
                correlation_id=request.correlation_id,
                outcome=EnumStandaloneRedeployOutcome.REFUSED,
                refusal=reason,
                message=message,
            )
        secret = os.environ.get(_SECRET_ENV, "").strip()
        if not secret:
            return ModelStandaloneServiceRedeployResult(
                correlation_id=request.correlation_id,
                outcome=EnumStandaloneRedeployOutcome.REFUSED,
                refusal=EnumStandaloneRedeployRefusal.SIGNING_KEY_NOT_SET,
                message="the deploy agent rejects unsigned commands and no signing key is set",
            )
        payload = _sign(
            {
                **request.model_dump(mode="json"),
                "correlation_id": str(request.correlation_id),
            },
            secret,
        )
        await self._bus.publish(
            self._topic,
            key=str(request.correlation_id).encode(),
            value=json.dumps(payload).encode(),
        )
        return ModelStandaloneServiceRedeployResult(
            correlation_id=request.correlation_id,
            outcome=EnumStandaloneRedeployOutcome.PUBLISHED,
            topic=self._topic,
            message=f"published recreate of {request.service!r} for the deploy agent",
        )
