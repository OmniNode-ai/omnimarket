# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains of the signing-key check of the branch deploy command effect, on the in-memory bus (OMN-24396).

* golden: the agent holds the node's key -> the command is signed with the agent's own rule and published once;
* error: the agent holds another key -> refused with nothing on the bus; then a bus that is down after a matching key ->
  failed, so the refusal and the failure stay different outcomes;
* http: with the overlay's own read (no injected reader), the replaced http boundary decides the outcome.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from omnimarket.nodes.node_branch_deploy_command_effect.adapters import (
    adapter_agent_key_check_http,
)
from omnimarket.nodes.node_branch_deploy_command_effect.handlers.handler_branch_deploy_command import (
    HandlerBranchDeployCommand,
)
from tests.nodes.node_branch_deploy_command_effect.support import (
    RUN_ID,
    SIGNING_ENV,
    SIGNING_KEY,
    FailingBus,
    FakeBus,
    FakeKeyCheckReader,
    matching_reader,
    overlay,
    request,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _signing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SIGNING_ENV, SIGNING_KEY)


async def test_golden_chain_matching_key_is_signed_and_published() -> None:
    bus = FakeBus()
    handler = HandlerBranchDeployCommand(
        event_bus=bus, overlay=overlay(), key_check_reader=matching_reader()
    )
    result = await handler.handle(request())

    assert result.status == "published"
    assert result.correlation_id == RUN_ID
    assert len(bus.published) == 1
    payload = json.loads(bus.published[0][2])
    body = {k: v for k, v in payload.items() if k != "_signature"}
    raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    assert (
        payload["_signature"]
        == hmac.new(SIGNING_KEY.encode(), raw, hashlib.sha256).hexdigest()
    )


async def test_error_chain_mismatching_key_refuses_then_down_bus_fails() -> None:
    refused_bus = FakeBus()
    refusing = HandlerBranchDeployCommand(
        event_bus=refused_bus,
        overlay=overlay(),
        key_check_reader=FakeKeyCheckReader("read", "0123456789abcdef"),
    )
    refused = await refusing.handle(request())
    assert refused.status == "refused"
    assert refused_bus.published == []

    failing = HandlerBranchDeployCommand(
        event_bus=FailingBus(), overlay=overlay(), key_check_reader=matching_reader()
    )
    failed = await failing.handle(request())
    assert failed.status == "failed"
    assert failed.detail == "ConnectionError"


async def test_overlay_read_with_a_replaced_http_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, float]] = []

    def agent_with_another_key(url: str, timeout: float) -> bytes:
        seen.append((url, timeout))
        return json.dumps({"hmac_key_check": "0123456789abcdef"}).encode()

    monkeypatch.setattr(
        adapter_agent_key_check_http, "fetch_health", agent_with_another_key
    )
    bus = FakeBus()
    handler = HandlerBranchDeployCommand(event_bus=bus, overlay=overlay())
    result = await handler.handle(request())

    assert result.status == "refused"
    assert bus.published == []
    configured = overlay()
    assert seen == [
        (configured.agent_health_url, configured.agent_health_timeout_seconds)
    ]
