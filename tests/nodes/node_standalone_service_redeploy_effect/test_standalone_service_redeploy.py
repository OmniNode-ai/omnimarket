# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The standalone service redeploy node: one declared service, dev lane only, over the bus."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml

from omnimarket.nodes.node_standalone_service_redeploy_effect.handlers.handler_standalone_service_redeploy import (
    HandlerStandaloneServiceRedeploy,
    load_declared_targets,
)
from omnimarket.nodes.node_standalone_service_redeploy_effect.models.model_standalone_service_redeploy import (
    EnumStandaloneRedeployOutcome,
    EnumStandaloneRedeployRefusal,
    ModelStandaloneServiceRedeployRequest,
)

NODE_DIR = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_standalone_service_redeploy_effect"
)
SECRET = "unit-test-secret"  # fixture value, not a credential


class FakeBus:
    def __init__(self) -> None:
        self.published: list[tuple[str, bytes, bytes]] = []

    async def publish(self, topic: str, *, key: bytes, value: bytes) -> None:
        self.published.append((topic, key, value))


@pytest.fixture(autouse=True)
def _secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEPLOY_AGENT_HMAC_SECRET", SECRET)


def _request(**overrides: Any) -> ModelStandaloneServiceRedeployRequest:
    target = load_declared_targets()[0]
    fields: dict[str, Any] = {
        "correlation_id": uuid4(),
        "requested_by": "unit-test",
        "runtime_lane": "dev",
        "compose_file": target.compose_file,
        "service": target.service,
        "git_ref": "origin/dev",
    }
    fields.update(overrides)
    return ModelStandaloneServiceRedeployRequest(**fields)


@pytest.mark.unit
async def test_declared_service_on_dev_is_published_signed_to_the_contract_topic() -> (
    None
):
    bus = FakeBus()
    request = _request()
    result = await HandlerStandaloneServiceRedeploy(bus).handle(request)

    assert result.outcome is EnumStandaloneRedeployOutcome.PUBLISHED
    assert len(bus.published) == 1
    topic, key, value = bus.published[0]
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    assert [topic] == contract["event_bus"]["publish_topics"]
    assert topic == "onex.cmd.deploy.standalone-service-recreate-requested.v1"
    assert key == str(request.correlation_id).encode()
    payload = json.loads(value)
    body = {k: v for k, v in payload.items() if k != "_signature"}
    assert body["compose_file"] == request.compose_file
    assert body["service"] == request.service
    expected = hmac.new(
        SECRET.encode(),
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode(),
        hashlib.sha256,
    ).hexdigest()
    assert payload["_signature"] == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        (
            {"compose_file": "docker/docker-compose.other.yml"},
            EnumStandaloneRedeployRefusal.COMPOSE_FILE_NOT_DECLARED,
        ),
        (
            {"service": "some-other-service"},
            EnumStandaloneRedeployRefusal.SERVICE_NOT_DECLARED,
        ),
        ({"runtime_lane": "prod"}, EnumStandaloneRedeployRefusal.LANE_NOT_ALLOWED),
        (
            {"runtime_lane": "canary"},
            EnumStandaloneRedeployRefusal.LANE_NOT_ALLOWED,
        ),
    ],
)
async def test_undeclared_target_or_lane_is_refused_and_nothing_is_published(
    overrides: dict[str, str], reason: EnumStandaloneRedeployRefusal
) -> None:
    bus = FakeBus()
    result = await HandlerStandaloneServiceRedeploy(bus).handle(_request(**overrides))

    assert result.outcome is EnumStandaloneRedeployOutcome.REFUSED
    assert result.refusal is reason
    assert bus.published == []


@pytest.mark.unit
async def test_a_missing_signing_key_is_refused_before_publishing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DEPLOY_AGENT_HMAC_SECRET")
    bus = FakeBus()
    result = await HandlerStandaloneServiceRedeploy(bus).handle(_request())
    assert result.outcome is EnumStandaloneRedeployOutcome.REFUSED
    assert result.refusal is EnumStandaloneRedeployRefusal.SIGNING_KEY_NOT_SET
    assert bus.published == []


@pytest.mark.unit
def test_contract_declares_dev_only_and_one_target() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    assert contract["allowed_lanes"] == ["dev"]
    assert len(contract["standalone_targets"]) == 1
